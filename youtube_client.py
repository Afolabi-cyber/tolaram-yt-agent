"""
youtube_client.py — All YouTube Data API v3 interactions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

class YouTubeClient:
    def __init__(self, api_key):
        self.youtube = build("youtube", "v3", developerKey=api_key)
        logger.info("YouTube API client initialised.")

    def get_channel_info(self, channel_id):
        try:
            r = self.youtube.channels().list(part="snippet,statistics,contentDetails", id=channel_id).execute()
            if not r.get("items"): return {}
            item = r["items"][0]
            stats, snippet = item.get("statistics", {}), item.get("snippet", {})
            return {
                "channel_id": channel_id, "title": snippet.get("title", "Unknown"),
                "description": snippet.get("description", "")[:200],
                "country": snippet.get("country", "N/A"),
                "published_at": snippet.get("publishedAt", ""),
                "subscriber_count": int(stats.get("subscriberCount", 0)),
                "video_count": int(stats.get("videoCount", 0)),
                "view_count": int(stats.get("viewCount", 0)),
                "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        except HttpError as e:
            logger.error(f"Channel fetch error {channel_id}: {e}"); return {}

    def get_recent_videos(self, channel_id, max_results=10):
        try:
            cr = self.youtube.channels().list(part="contentDetails", id=channel_id).execute()
            if not cr.get("items"): return []
            uploads_id = cr["items"][0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
            if not uploads_id: return []

            videos = []
            next_page_token = None
            while len(videos) < max_results:
                pr = self.youtube.playlistItems().list(
                    part="snippet,contentDetails",
                    playlistId=uploads_id,
                    maxResults=min(max_results - len(videos), 50),
                    pageToken=next_page_token
                ).execute()

                video_ids = [item["contentDetails"]["videoId"] for item in pr.get("items", [])]
                if video_ids:
                    videos.extend(self.get_video_details(video_ids, channel_id))

                next_page_token = pr.get("nextPageToken")
                if not next_page_token:
                    break

            return videos
        except HttpError as e:
            logger.error(f"Videos fetch error {channel_id}: {e}"); return []

    def get_video_details(self, video_ids, channel_id=""):
        try:
            r = self.youtube.videos().list(
                part="snippet,statistics,contentDetails", id=",".join(video_ids)
            ).execute()
            videos = []
            for item in r.get("items", []):
                stats, snippet, content = item.get("statistics", {}), item.get("snippet", {}), item.get("contentDetails", {})
                videos.append({
                    "video_id": item["id"], "channel_id": channel_id or snippet.get("channelId", ""),
                    "title": snippet.get("title", ""), "description": snippet.get("description", "")[:300],
                    "published_at": snippet.get("publishedAt", ""),
                    "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                    "duration": content.get("duration", ""),
                    "view_count": int(stats.get("viewCount", 0)),
                    "like_count": int(stats.get("likeCount", 0)),
                    "comment_count": int(stats.get("commentCount", 0)),
                    "tags": snippet.get("tags", [])[:5],
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })
            return videos
        except HttpError as e:
            logger.error(f"Video details error: {e}"); return []

    def get_comments(self, video_id, max_results=100):
        comments = []
        try:
            request = self.youtube.commentThreads().list(
                part="snippet", videoId=video_id,
                maxResults=min(max_results, 100), order="relevance", textFormat="plainText"
            )
            while request and len(comments) < max_results:
                response = request.execute()
                for item in response.get("items", []):
                    top = item["snippet"]["topLevelComment"]["snippet"]
                    comments.append({
                        "comment_id": item["id"], "video_id": video_id,
                        "author": top.get("authorDisplayName", "Anonymous"),
                        "text": top.get("textDisplay", ""),
                        "like_count": top.get("likeCount", 0),
                        "reply_count": item["snippet"].get("totalReplyCount", 0),
                        "published_at": top.get("publishedAt", ""),
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "category": None, "sentiment": None,
                        "priority": None, "suggested_reply": None, "crisis_flag": False,
                    })
                next_page = response.get("nextPageToken")
                if next_page and len(comments) < max_results:
                    request = self.youtube.commentThreads().list(
                        part="snippet", videoId=video_id,
                        maxResults=min(max_results - len(comments), 100),
                        order="relevance", textFormat="plainText", pageToken=next_page
                    )
                else: break
        except HttpError as e:
            if "commentsDisabled" in str(e): logger.info(f"Comments disabled for {video_id}")
            else: logger.error(f"Comments error {video_id}: {e}")
        logger.info(f"Fetched {len(comments)} comments for {video_id}")
        return comments
