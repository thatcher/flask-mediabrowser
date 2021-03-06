import os
import io
import logging
import mimetypes
from datetime import datetime
from functools import partial

from flask import Blueprint, render_template, abort, \
    url_for, Response, request, jsonify

from . import ffmpeg


class cached(object):
    """
    @cached(cache=cache, keyfunc=lambda path: path)
    def function(path):
        ...
    """

    def __init__(self, cache=None, keyfunc=None):
        assert cache, 'cache parameter must be set'
        assert keyfunc, 'keyfunc parameter must be set'
        self.cache = cache
        self.keyfunc = keyfunc

    def __call__(self, func):
        def wrapped_func(*args, **kwargs):
            key = self.keyfunc(*args, **kwargs)
            cached_value = self.cache.get(key)
            if cached_value is not None:
                return cached_value
            else:
                value = func(*args, **kwargs)
                self.cache.set(key, value)
                return value

        return wrapped_func


class cached_stream(object):
    """decorator to apply SavingIoWrapper"""

    def __init__(self, cache, keyfunc):
        self.cache = cache
        self.keyfunc = keyfunc

    def __call__(self, func):
        def wrapped_func(*args, **kwargs):
            key = self.keyfunc(*args, **kwargs)
            cached_value = self.cache.get(key)
            if cached_value is not None:
                return io.BytesIO(cached_value)
            else:
                value = func(*args, **kwargs)
                return SavingIoWrapper(value, key, self.cache)

        return wrapped_func


class SavingIoWrapper(io.RawIOBase):
    """Wraps a read-only io stream and buffers all read-ed data.
    on close() that data is written to the specified cache"""

    def __init__(self, stream, key, cache):
        self.stream = stream
        self.key = key
        self.cache = cache
        self.buf = b""
        self.finished = False

    def close(self):
        if self.finished:
            self.cache.set(self.key, self.buf)
            logging.info("Saved iostream after close to key {} with"
                         " length={}".format(self.key, len(self.buf)))
        self.stream.close()

    @property
    def closed(self):
        return self.stream.closed

    def readable(self):
        return self.stream.readable()

    def seekable(self):
        return False

    def read(self, size=-1):
        b = self.stream.read(size)
        self.buf += b
        if b == b'':
            self.finished = True
        return b


def build(root_directory, cache):
    blueprint = Blueprint('mediabrowser', __name__, static_folder='assets',
                          template_folder='templates')

    @cached(cache=cache, keyfunc=lambda ospath: ospath)
    def splittimes_cached(ospath):
        return list(ffmpeg.calculate_splittimes(ospath, 60))

    @cached(cache=cache, keyfunc=lambda path: "ffprobe_{}".format(path))
    def ffprobe(path):
        try:
            data = ffmpeg.ffprobe_data(path)
            if 'format' not in data or \
                            'duration' not in data['format']:
                logging.warning('analysis failed for %s: Incomplete data', path)
                return None
            else:
                return data
        except:
            logging.warning('ffprobe failed for %s', path)
            return None

    @cached(cache=cache, keyfunc=lambda path: "is_video_{}".format(path))
    def get_video_mime_type(path):
        """
        :return: mime type if path is video file or None otherwise
        """
        fallback = {
            '.mkv': 'video/x-matroska',
            '.avi': 'video/avi',
            '.webm': 'video/webm',
            '.flv': 'video/x-flv',
            '.mp4': 'video/mp4',
            '.mpg': 'video/MP2T'}

        (filetype, encoding) = mimetypes.guess_type(path)
        if filetype is None:
            _, extension = os.path.splitext(path)
            if extension in fallback.keys():
                return fallback[extension]
            else:
                return None
        else:
            if filetype.startswith('video/'):
                return filetype
            else:
                return None

    @cached_stream(cache=cache, keyfunc=lambda ospath: "thumb_video_{}".format(ospath))
    def ffmpeg_thumbnail_video(ospath):
        process = ffmpeg.thumbnail_video(ospath, 100, 60)
        return process.stdout

    @cached_stream(cache=cache, keyfunc=lambda ospath: "thumb_poster_{}".format(ospath))
    def ffmpeg_thumbnail_poster(ospath):
        process = ffmpeg.poster(ospath)
        return process.stdout

    @blueprint.route('/<path:path>/stream/<float:ss>_<float:t>')
    def stream(ss, t, path):
        path = os.path.normpath(path)
        ospath = os.path.join(root_directory, path)
        data = ffprobe(ospath)
        duration = float(data['format']['duration'])
        # cut at next key frame after given time 'ss'
        _, new_ss = ffmpeg.find_next_keyframe(ospath, ss, t / 2)

        if ss + t * 2 > duration:
            # encode all remain frames at once
            new_t = duration - new_ss
        else:
            # find next key frame after given time 't'
            new_t_prev_duration, new_t = ffmpeg.find_next_keyframe(ospath, ss + t, t / 2)
            new_t -= new_ss
            # minus one frame
            # new_t -= new_t_prev_duration

        process = ffmpeg.stream(ospath, new_ss, new_t)
        return Response(process.stdout, mimetype='video/MP2T')

    @blueprint.route('/<path:path>/m3u8')
    def m3u8(path):
        path = os.path.normpath(path)
        ospath = os.path.join(root_directory, path)

        max_chunk_duration = 60
        splittimes = splittimes_cached(ospath)

        buf = '#EXTM3U\n'
        buf += '#EXT-X-VERSION:3\n'
        buf += '#EXT-X-TARGETDURATION:{}\n'.format(max_chunk_duration)
        buf += '#EXT-X-MEDIA-SEQUENCE:0\n'

        for (pos, chunk_duration) in splittimes:
            buf += "#EXTINF:{},\n".format(chunk_duration)
            buf += "stream/{}_{}\n".format(pos, chunk_duration)

        buf += '#EXT-X-ENDLIST\n'
        return Response(buf, mimetype='application/x-mpegurl')

    @blueprint.route('/<path:path>/poster')
    def poster(path):
        path = os.path.normpath(path)
        ospath = os.path.join(root_directory, path)
        client_mtime = request.if_modified_since
        mtime = datetime.fromtimestamp(os.stat(ospath).st_mtime)
        if client_mtime is not None and mtime <= client_mtime:
            return Response(status=304)
        else:
            stream = ffmpeg_thumbnail_poster(ospath)
            r = Response(stream, mimetype="image/jpeg")
            r.last_modified = mtime
            return r

    @blueprint.route('/<path:path>/thumbnail_video')
    def thumbnail_video(path):
        path = os.path.normpath(path)
        ospath = os.path.join(root_directory, path)
        client_mtime = request.if_modified_since
        mtime = datetime.fromtimestamp(os.stat(ospath).st_mtime)
        if client_mtime is not None and mtime <= client_mtime:
            return Response(status=304)
        else:
            stream = ffmpeg_thumbnail_video(ospath)
            r = Response(stream, mimetype="video/webm")
            r.last_modified = mtime
            return r

    @blueprint.route('/<path:path>/download/inline')
    def download_inline(path):
        return download(path, inline=True)

    @blueprint.route('/<path:path>/download')
    def download(path, inline=False):
        path = os.path.normpath(path)
        ospath = os.path.join(root_directory, path)
        filename = os.path.basename(path)
        mime_type = get_video_mime_type(ospath)
        if not mime_type:
            return Response(status=501, response=b'Not a video file')

        r = Response(open(ospath, 'rb'), mimetype=mime_type)
        if inline:
            r.headers['Content-Disposition'] = "inline; filename=\"{}\"".format(filename)
        else:
            r.headers['Content-Disposition'] = "attachment; filename=\"{}\"".format(filename)

        return r

    @blueprint.route('/<path:path>/watch')
    def watch(path):
        path = os.path.normpath(path)
        filename = os.path.basename(path)
        return render_template('watch.html',
                               path=path, filename=filename)

    @blueprint.route('/', defaults={'path': ''})
    @blueprint.route('/<path:path>/list')
    def listdir(path):
        def gather_fileinfo(path, ospath, filename):
            osfilepath = os.path.join(ospath, filename)
            if os.path.isdir(osfilepath) and not filename.startswith('.'):
                return {'type': 'directory', 'filename': filename,
                        'link': url_for('mediabrowser.listdir',
                                        path=os.path.join(path, filename))}
            else:
                if not get_video_mime_type(osfilepath):
                    return None
                else:
                    return {
                        'type': 'file', 'filename': filename,
                        'fullpath': os.path.join(path, filename)}

        try:
            path = os.path.normpath(path)
            ospath = os.path.join(root_directory, path)
            files = list(
                map(partial(gather_fileinfo, path, ospath), os.listdir(ospath)))
            files = list(filter(lambda file: file is not None, files))
            files.sort(key=lambda i: (i['type'] == 'file' and '1' or '0') + i['filename'].lower())
            return render_template('listdir.html',
                                   files=files,
                                   parent=os.path.dirname(path),
                                   path=path)
        except FileNotFoundError:
            abort(404)

    @blueprint.route('/json/', defaults={'path': ''})
    @blueprint.route('/json/<path:path>')
    def json(path):
        def gather_fileinfo(path, ospath, filename):
            osfilepath = os.path.join(ospath, filename)
            if os.path.isdir(osfilepath) and not filename.startswith('.'):
                return {'type': 'directory',
                        'name': filename,
                        'path': url_for('mediabrowser.json',
                                        path=os.path.join(path, filename))}
            else:
                if not get_video_mime_type(osfilepath):
                    return None
                else:
                    return {'type': 'file',
                            'name': filename,
                            'poster': url_for('mediabrowser.poster',
                                              path=os.path.join(path, filename)),
                            'm3u8': url_for('mediabrowser.m3u8',
                                            path=os.path.join(path, filename))}

        try:
            path = os.path.normpath(path)
            ospath = os.path.join(root_directory, path)
            files = list(filter(lambda it: it is not None,
                map(partial(gather_fileinfo, path, ospath), os.listdir(ospath))))
            return jsonify({'files': files})
        except FileNotFoundError:
            abort(404)

    return blueprint
