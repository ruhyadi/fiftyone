"""
Utilities for working with `YouTube <https://youtube.com>`.

| Copyright 2017-2022, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
import logging
import multiprocessing
import multiprocessing.dummy
import os

import numpy as np

import eta.core.utils as etau
import eta.core.video as etav

import fiftyone.core.utils as fou

pytube = fou.lazy_import(
    "pytube", callback=lambda: fou.ensure_package("pytube"),
)


logger = logging.getLogger(__name__)


def download_youtube_videos(
    urls,
    download_dir=None,
    video_paths=None,
    clip_segments=None,
    ext=None,
    resolution="highest",
    max_videos=None,
    num_workers=None,
    skip_failures=True,
):
    """Downloads a list of YouTube videos.

    The ``urls`` argument accepts a list of YouTube "watch" URLs::

        urls = [
            "https://www.youtube.com/watch?v=-0URMJE8_PB",
            ...,
        ]

    Use either the ``download_dir`` or ``video_paths`` argument to specify
    where to download each video.

    You can use the optional ``clip_segments`` argument to specify a specific
    segment, in seconds, of each video to download::

        clip_segments = [
            (10, 25),
            (11.1, 20.2),
            None,               # entire video
            (8.0, None),        # through end of video
            ...
        ]

    You can also use the optional ``ext`` and ``resolution`` arguments to
    specify a deisred video codec and resolution to download, if possible.

    YouTube videos are regularly taken down. Therefore, this method provides an
    optional ``max_videos`` argument that you can use in conjunction with
    ``skip_failures=True`` and a large list of possibly non-existent videos in
    ``urls`` in cases where you need a certain number of videos to be
    successfully downloaded but are willing to tolerate failures.

    Args:
        urls: a list of YouTube URLs to download
        download_dir (None): a directory in which to store the downloaded
            videos
        video_paths (None): a list of paths to which to download the videos.
            When downloading entire videos, a stream matching the video format
            implied by each file's extension is downloaded, if available, or
            else the extension of the video path is **changed** to match the
            available stream's format
        clip_segments (None): a list of ``(first, last)`` tuples defining a
            specific segment of each video to download
        ext (None): an optional video format like ``".mp4"`` to download for
            each video, if possible. Only applicable when a ``download_dir`` is
            used. This format will be respected if such a stream exists,
            otherwise the format of the best available stream is used
        resolution (None): a desired stream resolution to download. The
            supported values are:

            -   ``"highest"`` (default): download the highest resolution stream
            -   ``"lowest"``:  download the lowest resolution stream
            -   A target resolution like ``"1080p"``. In this case, the stream
                whose resolution is closest to this target value is downloaded
        max_videos (None): the maximum number of videos to successfully
            download. By default, all videos are be downloaded
        num_workers (None): the number of threads or processes to use when
            downloading videos. By default, ``multiprocessing.cpu_count()`` is
            used
        skip_failures (True): whether to gracefully continue without raising
            an error if a video cannot be downloaded

    Returns:
        a tuple of

        -   **downloaded**: a dict mapping integer indexes into ``urls`` to
            paths of successfully downloaded videos
        -   **errors**: a dict mapping integer indexes into ``urls`` to error
            messages for videos that were attempted to be downloaded, but
            failed
    """
    use_threads = clip_segments is None
    num_workers = _parse_num_workers(num_workers, use_threads=use_threads)

    if max_videos is None:
        max_videos = len(urls)

    with etau.TempDir() as tmp_dir:
        tasks = _build_tasks_list(
            urls,
            download_dir,
            video_paths,
            clip_segments,
            tmp_dir,
            ext,
            resolution,
        )

        if num_workers <= 1:
            downloaded, errors = _download(tasks, max_videos, skip_failures)
        else:
            downloaded, errors = _download_multi(
                tasks, max_videos, num_workers, skip_failures, use_threads
            )

    return downloaded, errors


def _parse_num_workers(num_workers, use_threads=False):
    if num_workers is None:
        if os.name == "nt" and not use_threads:
            # Multiprocessing on Windows is bad news
            return 1

        return multiprocessing.cpu_count()

    return num_workers


def _build_tasks_list(
    urls, download_dir, video_paths, clip_segments, tmp_dir, ext, resolution
):
    if video_paths is None and download_dir is None:
        raise ValueError("Either `download_dir` or `video_paths` are required")

    if video_paths is not None and ext is not None:
        logger.warning("Ignoring ext=%s when `video_paths` are provided", ext)
        ext = None

    if not etau.is_str(resolution) or (
        resolution not in ("highest", "lowest")
        and not resolution.endswith("p")
    ):
        raise ValueError(
            "Invalid resolution=%s. The supported values are 'highest', "
            "'lowest', '1080p', '720p', ..." % resolution
        )

    if resolution.endswith("p"):
        resolution = int(resolution[:-1])

    num_videos = len(urls)

    if clip_segments is None:
        clip_segments = [None] * num_videos
    else:
        clip_segments = list(clip_segments)
        for idx, clip_segment in enumerate(clip_segments):
            if (
                clip_segment is not None
                and (clip_segment[0] is None or clip_segment[0] <= 0)
                and clip_segment[1] is None
            ):
                clip_segments[idx] = None

    download_dir_list = _to_list(download_dir, num_videos)
    video_paths_list = _to_list(video_paths, num_videos)
    tmp_dir_list = _to_list(tmp_dir, num_videos)
    ext_list = _to_list(ext, num_videos)
    resolution_list = _to_list(resolution, num_videos)
    return list(
        zip(
            range(num_videos),
            urls,
            download_dir_list,
            video_paths_list,
            clip_segments,
            tmp_dir_list,
            ext_list,
            resolution_list,
        )
    )


def _to_list(arg, n):
    return list(arg) if etau.is_container(arg) else [arg] * n


def _download(tasks, max_videos, skip_failures):
    downloaded = {}
    errors = {}

    with fou.ProgressBar(total=max_videos, iters_str="videos") as pb:
        for task in tasks:
            idx, url, video_path, error = _do_download(task)
            if error:
                if not skip_failures:
                    raise ValueError(
                        "Failed to download video '%s'\nError: %s"
                        % (url, error)
                    )

                errors[idx] = error
            else:
                pb.update()
                downloaded[idx] = video_path
                if len(downloaded) >= max_videos:
                    return downloaded, errors

    return downloaded, errors


def _download_multi(
    tasks, max_videos, num_workers, skip_failures, use_threads
):
    downloaded = {}
    errors = {}

    with fou.ProgressBar(total=max_videos, iters_str="videos") as pb:
        if use_threads:
            pool_cls = multiprocessing.dummy.Pool
        else:
            pool_cls = multiprocessing.Pool

        with pool_cls(num_workers) as pool:
            for idx, url, video_path, error in pool.imap_unordered(
                _do_download, tasks
            ):
                if error:
                    if not skip_failures:
                        raise ValueError(
                            "Failed to download video '%s'\nError: %s"
                            % (url, error)
                        )

                    errors[idx] = error
                else:
                    pb.update()
                    downloaded[idx] = video_path
                    if len(downloaded) >= max_videos:
                        return downloaded, errors

    return downloaded, errors


def _do_download(task):
    (
        idx,
        url,
        download_dir,
        video_path,
        clip_segment,
        tmp_dir,
        ext,
        resolution,
    ) = task

    try:
        pytube_video = pytube.YouTube(url)

        error = _is_playable(pytube_video)
        if error:
            return idx, url, None, error

        if video_path is not None and ext is None:
            ext = os.path.splitext(video_path)

        stream = _get_stream(pytube_video, ext, resolution)
        if stream is None:
            return idx, url, None, "No stream found"

        if video_path is None:
            filename = stream.default_filename
            if ext is not None:
                filename = os.path.splitext(filename)[0] + ext

            video_path = os.path.join(download_dir, filename)

        root, ext = os.path.splitext(video_path)[1]
        stream_ext = os.path.splitext(stream.default_filename)[1]
        if ext != stream_ext:
            logger.warning(
                "Unable to download '%s' to video format '%s'; using "
                "'%s' instead",
                url,
                ext,
                stream_ext,
            )
            video_path = root + stream_ext

        # Download to a temporary location first and then move to `video_path`
        # so that only successful downloads end up at their final destination
        tmp_path = os.path.join(tmp_dir, os.path.basename(video_path))

        if clip_segment is None:
            _download_video(stream, tmp_path)
        else:
            _download_clip(stream, clip_segment, tmp_path)

        etau.move_file(tmp_path, video_path)

        return idx, url, video_path, None
    except Exception as e:
        if isinstance(e, pytube.exceptions.PytubeError):
            error = type(e)
        else:
            error = str(e)

        return idx, url, None, error


def _is_playable(pytube_video):
    status, messages = pytube.extract.playability_status(
        pytube_video.watch_html
    )

    if status is None:
        return None

    if not etau.is_container(messages):
        return messages

    if messages:
        return messages[0]

    return status


def _get_stream(pytube_video, ext, resolution):
    # Try to download an audio + video stream, if possible
    progressive = True

    # If the user didn't request a particular format, try to find MP4 first
    if ext is None:
        ext = ".mp4"

    while True:
        streams = pytube_video.streams.filter(
            type="video", progressive=progressive, file_extension=ext[1:]
        )

        if streams:
            if etau.is_numeric(resolution):
                all_res = [int(s.resolution[:-1]) for s in streams]
                idx = _find_nearest(all_res, resolution)
                return streams[idx]

            if resolution == "lowest":
                return streams.order_by("resolution").first()

            return streams.order_by("resolution").desc().first()

        if progressive:
            progressive = False
        elif ext is not None:
            ext = None
        else:
            return None


def _find_nearest(array, target):
    return np.argmin(np.abs(np.asarray(array) - target))


def _download_video(stream, video_path):
    outdir, filename = os.path.split(video_path)
    stream.download(output_path=outdir, filename=filename)


def _download_clip(stream, clip_segment, video_path):
    if clip_segment is None:
        clip_segment = (None, None)

    start_time, end_time = clip_segment

    if start_time is None:
        start_time = 0

    if end_time is not None:
        duration = end_time - start_time
    else:
        duration = None

    etav.extract_clip(
        stream.url, video_path, start_time=start_time, duration=duration
    )
