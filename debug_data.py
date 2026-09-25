import json
from pathlib import Path

data_dir = Path(__file__).parent / 'data'
comments_file = data_dir / 'all_comments.json'

with open(comments_file, 'r', encoding='utf-8') as f:
    comments = json.load(f)

for c in comments:
    pub = c.get("published_at", "")
    if isinstance(pub, dict):
        print(f"Bad published_at in comment: {c.get('comment_id')}")
        break

videos_file = data_dir / 'all_videos.json'
with open(videos_file, 'r', encoding='utf-8') as f:
    videos = json.load(f)

for v in videos:
    pub = v.get("published_at", "")
    if isinstance(pub, dict) or isinstance(v.get("view_count", 0), str):
        print(f"Bad type in video: {v.get('video_id')}")
        break

print(f"Loaded {len(comments)} comments and {len(videos)} videos.")
