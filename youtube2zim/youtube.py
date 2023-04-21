#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4 nu

import requests
import yt_dlp

from contextlib import ExitStack
from dateutil import parser as dt_parser
from datetime import datetime
from pytube import extract
from zimscraperlib.download import stream_file
from zimscraperlib.image.transformation import resize_image

from .constants import CHANNEL, PLAYLIST, USER, YOUTUBE, logger
from .utils import get_slug, load_json, save_json

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
PLAYLIST_API = f"{YOUTUBE_API}/playlists"
PLAYLIST_ITEMS_API = f"{YOUTUBE_API}/playlistItems"
CHANNEL_SECTIONS_API = f"{YOUTUBE_API}/channelSections"
CHANNELS_API = f"{YOUTUBE_API}/channels"
SEARCH_API = f"{YOUTUBE_API}/search"
VIDEOS_API = f"{YOUTUBE_API}/videos"
MAX_VIDEOS_PER_REQUEST = 50  # for VIDEOS_API
RESULTS_PER_PAGE = 50  # max: 50


class Playlist:
    def __init__(self, playlist_id, title, description, creator_id, creator_name):
        self.playlist_id = playlist_id
        self.title = title
        self.description = description
        self.creator_id = creator_id
        self.creator_name = creator_name
        self.slug = get_slug(title, js_safe=True)

    @classmethod
    def from_id(cls, playlist_id):
        playlist_json = get_playlist_json(playlist_id)
        return Playlist(
            playlist_id=playlist_id,
            title=playlist_json["snippet"]["title"],
            description=playlist_json["snippet"]["description"],
            creator_id=playlist_json["snippet"]["channelId"],
            creator_name=playlist_json["snippet"]["channelTitle"],
        )

    def __dict__(self):
        return {
            "playlist_id": self.playlist_id,
            "title": self.title,
            "description": self.description,
            "creator_id": self.creator_id,
            "creator_name": self.creator_name,
            "slug": self.slug.replace("_", "-"),
        }


def credentials_ok():
    """check that a Youtube search is successful, validating API_KEY"""
    req = requests.get(
        SEARCH_API, params={"part": "snippet", "maxResults": 1, "key": YOUTUBE.api_key}
    )
    if req.status_code > 400:
        logger.error(f"HTTP {req.status_code} Error response: {req.text}")
    try:
        req.raise_for_status()
        return bool(req.json()["items"])
    except Exception:
        return False


def get_channel_json(channel_id, for_username=False):
    """fetch or retieve-save and return the Youtube ChannelResult JSON"""
    fname = f"channel_{channel_id}"
    channel_json = load_json(YOUTUBE.cache_dir, fname)
    if channel_json is None:
        logger.debug(f"query youtube-api for Channel #{channel_id}")
        req = requests.get(
            CHANNELS_API,
            params={
                "forUsername" if for_username else "id": channel_id,
                "part": "brandingSettings,snippet,contentDetails",
                "key": YOUTUBE.api_key,
            },
        )
        if req.status_code > 400:
            logger.error(f"HTTP {req.status_code} Error response: {req.text}")
        req.raise_for_status()
        try:
            channel_json = req.json()["items"][0]
        except (KeyError, IndexError):
            if for_username:
                logger.error(f"Invalid username `{channel_id}`: Not Found")
            else:
                logger.error(f"Invalid channelId `{channel_id}`: Not Found")
            raise
        save_json(YOUTUBE.cache_dir, fname, channel_json)
    return channel_json


def get_channel_playlists_json(channel_id):
    """fetch or retieve-save and return the Youtube Playlists JSON for a channel"""
    fname = f"channel_{channel_id}_playlists"
    channel_playlists_json = load_json(YOUTUBE.cache_dir, fname)

    items = load_json(YOUTUBE.cache_dir, fname)
    if items is not None:
        return items

    logger.debug(f"query youtube-api for Playlists of channel #{channel_id}")

    items = []
    page_token = None
    while True:
        req = requests.get(
            PLAYLIST_API,
            params={
                "channelId": channel_id,
                "part": "id",
                "key": YOUTUBE.api_key,
                "maxResults": RESULTS_PER_PAGE,
                "pageToken": page_token,
            },
        )
        if req.status_code > 400:
            logger.error(f"HTTP {req.status_code} Error response: {req.text}")
        req.raise_for_status()
        channel_playlists_json = req.json()
        items += channel_playlists_json["items"]
        save_json(YOUTUBE.cache_dir, fname, items)
        page_token = channel_playlists_json.get("nextPageToken")
        if not page_token:
            break
    return items


def get_playlist_json(playlist_id):
    """fetch or retieve-save and return the Youtube PlaylistResult JSON"""
    fname = f"playlist_{playlist_id}"
    playlist_json = load_json(YOUTUBE.cache_dir, fname)
    if playlist_json is None:
        logger.debug(f"query youtube-api for Playlist #{playlist_id}")
        req = requests.get(
            PLAYLIST_API,
            params={"id": playlist_id, "part": "snippet", "key": YOUTUBE.api_key},
        )
        if req.status_code > 400:
            logger.error(f"HTTP {req.status_code} Error response: {req.text}")
        req.raise_for_status()
        try:
            playlist_json = req.json()["items"][0]
        except IndexError:
            logger.error(f"Invalid playlistId `{playlist_id}`: Not Found")
            raise
        save_json(YOUTUBE.cache_dir, fname, playlist_json)
    return playlist_json


def get_videos_json(playlist_id):
    """retrieve a list of youtube PlaylistItem dict

    same request for both channel and playlist
    channel mode uses `uploads` playlist from channel"""

    fname = f"playlist_{playlist_id}_videos"
    items = load_json(YOUTUBE.cache_dir, fname)
    if items is not None:
        return items

    logger.debug(f"query youtube-api for PlaylistItems of playlist #{playlist_id}")

    items = []
    page_token = None
    while True:
        req = requests.get(
            PLAYLIST_ITEMS_API,
            params={
                "playlistId": playlist_id,
                "part": "snippet,contentDetails,status",
                "key": YOUTUBE.api_key,
                "maxResults": RESULTS_PER_PAGE,
                "pageToken": page_token,
            },
        )
        if req.status_code > 400:
            logger.error(f"HTTP {req.status_code} Error response: {req.text}")
        req.raise_for_status()
        videos_json = req.json()
        items += videos_json["items"]
        page_token = videos_json.get("nextPageToken")
        if not page_token:
            break

    save_json(YOUTUBE.cache_dir, fname, items)
    return items

def subset_videos_json(videos, subset_by, subset_videos, subset_gb):
    """filter the videos by a subset of videos"""
    options = {
            "ignoreerrors": True,
        }
    # query the youtube api for the video statistics
    video_ids = [video["contentDetails"]["videoId"] for video in videos.values()]
    video_stats = {}
    for i in range(0, len(video_ids), 50):
        video_ids_chunk = video_ids[i : i + 50]
        req = requests.get(
            VIDEOS_API,
            params={
                "id": ",".join(video_ids_chunk),
                "part": "statistics",
                "key": YOUTUBE.api_key,
            },
        )
        if req.status_code > 400:
            logger.error(f"HTTP {req.status_code} Error response: {req.text}")
        req.raise_for_status()
        video_stats_json = req.json()
        for video in video_stats_json["items"]:
            video_stats[video["id"]] = video["statistics"]
    # we add the statistics to the videos
    for video in videos.values():
        video["statistics"] = video_stats[video["contentDetails"]["videoId"]]
    # we sort the videos by views or recent or views-per-year
    if subset_by == "views":
        videos = list(videos.values())
        videos = sorted(videos, key=lambda video: video["statistics"]["viewCount"], reverse=True)
    elif subset_by == "recent":
        videos = list(videos.values())
        videos = sorted(videos, key=lambda video: video["snippet"]["publishedAt"], reverse=True)
    elif subset_by == "views-per-year":
        for video in videos.values():
            views = video["statistics"]["viewCount"]
            published_at = video["snippet"]["publishedAt"]
            now = datetime.now()
            published_at = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")
            years = now.year - published_at.year
            video["statistics"]["views_per_year"] = int(views) / (years + 1)
        videos = list(videos.values())
        videos = sorted(videos, key=lambda video: video["statistics"]["views_per_year"], reverse=True)
    if subset_videos != 0:
        videos_ids = [video["contentDetails"]["videoId"] for video in videos]
        videos_ids_subset = videos_ids[:subset_videos]
        videos = [video for video in videos if video["contentDetails"]["videoId"] in videos_ids_subset]
    if subset_gb != 0:
        total_size = 0
        videos_ids_subset = []
        for video in videos:
            video_id = video["contentDetails"]["videoId"]
            video_size = yt_dlp.YoutubeDL(options).extract_info(
                video_id, download=False
            )["filesize_approx"] / 1024 / 1024 / 1024
            if total_size + video_size <= subset_gb:
                total_size += video_size
                videos_ids_subset.append(video_id)
                if video_id == videos[-1]["contentDetails"]["videoId"]:
                    videos_ids = videos_ids_subset
                    videos = [video for video in videos if video["contentDetails"]["videoId"] in videos_ids]
                    break
            else:
                videos_ids = videos_ids_subset
                videos = [video for video in videos if video["contentDetails"]["videoId"] in videos_ids]
                break
    return videos


# Replace some video titles reading 2 text files, one for the video id and one for the title (called with --custom-titles)
def replace_titles(items, custom_titles):
    """replace video titles with custom titles from file"""
    # get the list of custom titles files
    logger.debug(f"found {len(custom_titles)} custom titles files")
    # raise an error if there are not exactly 2 custom titles files
    if len(custom_titles) == 0:
        logger.error("no custom titles files found")
        raise ValueError("no custom titles files found")
    elif len(custom_titles) == 1:
        logger.error("only one custom titles file found (need one for titles and one for ids)")
        raise ValueError("only one custom titles file found")
    elif len(custom_titles) > 2:
        logger.error("too many custom titles files found (need one for titles and one for ids)")
        raise ValueError("too many custom titles files found")
    custom_titles_files = custom_titles
    titles = []
    ids = []

    # iterate through the files in custom_titles_files
    with ExitStack() as stack:
        files = [stack.enter_context(open(fname)) for fname in custom_titles_files]
        for f in files:
            logger.debug(f"found {len(f.readlines())} custom titles in {f.name}")
            f.seek(0)
            for line in f:
                if line.startswith("https://"):
                    ids.append(extract.video_id(line))
                    logger.debug(f"found video id {ids[-1]}")
                else:
                    titles.append(line.rstrip())
                    logger.debug(f"found title {titles[-1]}")
        
        # check that the number of titles and ids are the same
        if len(titles) != len(ids):
            logger.error(
                f"number of titles ({len(titles)}) and ids ({len(ids)}) do not match"
            )
            raise ValueError("number of titles and ids do not match")
        
        # check if there are duplicate ids
        if len(ids) != len(set(ids)):
            logger.error(f"duplicate ids found: {item for item, count in collections.Counter(ids).items() if count > 1}")

    # iterate through the json file and replace the title with the title from the list of titles
    v_index = 0
    for item in items:
        if v_index < len(ids):
            while v_index < len(ids) and item["contentDetails"]["videoId"] != ids[v_index]:
                v_index += 1
            if v_index < len(ids):
                logger.info(f"replacing {item['snippet']['title']} with {titles[v_index]}")
                item["snippet"]["title"] = titles[v_index]
                v_index += 1
        else:
            logger.debug(f"no more titles to replace")
            break


def get_videos_authors_info(videos_ids):
    """query authors' info for each video from their relative channel"""

    items = load_json(YOUTUBE.cache_dir, "videos_channels")

    if items is not None:
        return items

    logger.debug(
        "query youtube-api for Video details of {} videos".format(len(videos_ids))
    )

    items = {}

    def retrieve_videos_for(videos_ids):
        """{videoId: {channelId: channelTitle}} for all videos_ids"""
        req_items = {}
        page_token = None
        while True:
            req = requests.get(
                VIDEOS_API,
                params={
                    "id": ",".join(videos_ids),
                    "part": "snippet",
                    "key": YOUTUBE.api_key,
                    "maxResults": RESULTS_PER_PAGE,
                    "pageToken": page_token,
                },
            )
            if req.status_code > 400:
                logger.error(f"HTTP {req.status_code} Error response: {req.text}")
            req.raise_for_status()
            videos_json = req.json()
            for item in videos_json["items"]:
                req_items.update(
                    {
                        item["id"]: {
                            "channelId": item["snippet"]["channelId"],
                            "channelTitle": item["snippet"]["channelTitle"],
                        }
                    }
                )
            page_token = videos_json.get("nextPageToken")
            if not page_token:
                break
        return req_items

    # split it over n requests so that each request includes
    # as most MAX_VIDEOS_PER_REQUEST videoId to avoid too-large URI issue
    for interv in range(0, len(videos_ids), MAX_VIDEOS_PER_REQUEST):
        items.update(
            retrieve_videos_for(videos_ids[interv : interv + MAX_VIDEOS_PER_REQUEST])
        )

    save_json(YOUTUBE.cache_dir, "videos_channels", items)

    return items


def save_channel_branding(channels_dir, channel_id, save_banner=False):
    """download, save and resize profile [and banner] of a channel"""
    channel_json = get_channel_json(channel_id)

    thumbnails = channel_json["snippet"]["thumbnails"]
    for quality in ("medium", "default"):  # high:800px, medium:240px, default:88px
        if quality in thumbnails.keys():
            thumnbail = thumbnails[quality]["url"]
            break

    channel_dir = channels_dir.joinpath(channel_id)
    channel_dir.mkdir(exist_ok=True)

    profile_path = channel_dir.joinpath("profile.jpg")
    if not profile_path.exists():
        stream_file(thumnbail, profile_path)
        # resize profile as we only use up 100px/80 sq
        resize_image(profile_path, width=100, height=100)

    # currently disabled as per deprecation of the following property
    # without an alternative way to retrieve it (using the API)
    # See: https://developers.google.com/youtube/v3/revision_history#september-9,-2020
    if save_banner and False:
        banner = channel_json["brandingSettings"]["image"]["bannerImageUrl"]
        banner_path = channel_dir.joinpath("banner.jpg")
        if not banner_path.exists():
            stream_file(banner, banner_path)


def skip_deleted_videos(item):
    """filter func to filter-out deleted, unavailable or private videos"""
    return (
        item["snippet"]["title"] != "Deleted video"
        and item["snippet"]["description"] != "This video is unavailable."
        and item["status"]["privacyStatus"] != "private"  
    )


def skip_outofrange_videos(date_range, item):
    """filter func to filter-out videos that are not within specified date range"""
    return dt_parser.parse(item["snippet"]["publishedAt"]).date() in date_range


def extract_playlists_details_from(collection_type, youtube_id):
    """prepare a list of Playlist from user request

    USER: we fetch the hidden channel associate to it
    CHANNEL (and USER): we grab all playlists + `uploads` playlist
    PLAYLIST: we retrieve from the playlist Id(s)"""

    uploads_playlist_id = None
    main_channel_id = None
    if collection_type == USER or collection_type == CHANNEL:
        if collection_type == USER:
            # youtube_id is a Username, fetch actual channelId through channel
            channel_json = get_channel_json(youtube_id, for_username=True)
        else:
            # youtube_id is a channelId
            channel_json = get_channel_json(youtube_id)

        main_channel_id = channel_json["id"]

        # retrieve list of playlists for that channel
        playlist_ids = [p["id"] for p in get_channel_playlists_json(main_channel_id)]
        # we always include uploads playlist (contains everything)
        playlist_ids += [channel_json["contentDetails"]["relatedPlaylists"]["uploads"]]
        uploads_playlist_id = playlist_ids[-1]
    elif collection_type == PLAYLIST:
        playlist_ids = youtube_id.split(",")
        main_channel_id = Playlist.from_id(playlist_ids[0]).creator_id
    else:
        raise NotImplementedError("unsupported collection_type")

    return (
        [Playlist.from_id(playlist_id) for playlist_id in list(set(playlist_ids))],
        main_channel_id,
        uploads_playlist_id,
    )
