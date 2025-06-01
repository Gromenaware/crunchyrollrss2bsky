import arrow
import fastfeedparser
import json
import os
import logging
import re
import httpx

from atproto import Client, client_utils, models
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# --- Config ---
CONFIG_PATH = os.environ.get("RSS2BSKY_CONFIG", "config.json")
LOG_PATH = os.environ.get("RSS2BSKY_LOG", "rss2bsky.log")

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s %(message)s",
    filename=LOG_PATH,
    encoding="utf-8",
    level=logging.INFO,
)

def fetch_link_metadata(url):
    try:
        r = httpx.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        title = (soup.find("meta", property="og:title") or soup.find("title"))
        desc = (soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"}))
        image = (soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"}))
        return {
            "title": title["content"] if title and title.has_attr("content") else (title.text if title else ""),
            "description": desc["content"] if desc and desc.has_attr("content") else "",
            "image": image["content"] if image and image.has_attr("content") else None,
        }
    except Exception as e:
        logging.warning(f"Could not fetch link metadata for {url}: {e}")
        return {}

def get_last_bsky(client, handle):
    timeline = client.get_author_feed(handle)
    for titem in timeline.feed:
        # Only care about top-level, non-reply posts
        if titem.reason is None and getattr(titem.post.record, "reply", None) is None:
            logging.info("Record created %s", str(titem.post.record.created_at))
            return arrow.get(titem.post.record.created_at)
    return arrow.get(0)

def make_rich(content):
    text_builder = client_utils.TextBuilder()
    lines = content.split("\n")
    for line in lines:
        # If the line is a URL, make it a clickable link
        if line.startswith("http"):
            url = line.strip()
            url_obj = urlparse(url)
            text_builder.link(url, url)
        else:
            tag_split = re.split("(#[a-zA-Z0-9]+)", line)
            for i, t in enumerate(tag_split):
                if i == len(tag_split) - 1:
                    t = t + "\n"
                if t.startswith("#"):
                    text_builder.tag(t, t[1:].strip())
                else:
                    text_builder.text(t)
    return text_builder

def get_image_from_url(image_url, client):
    try:
        r = httpx.get(image_url)
        if r.status_code != 200:
            return None
        img_blob = client.upload_blob(r.content)
        img_model = models.AppBskyEmbedImages.Image(
            alt="Preview image", image=img_blob.blob
        )
        return img_model
    except Exception as e:
        logging.warning(f"Could not fetch/upload image from {image_url}: {e}")
        return None

def main():
    # --- Load config ---
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    offline = config.get("bsky", {}).get("handle") == "offline"

    # --- Login ---
    client = Client()
    if not offline:
        import time
        backoff = 60
        while True:
            try:
                client.login(config["bsky"]["username"], config["bsky"]["password"])
                break
            except Exception as e:
                logging.exception("Login exception")
                time.sleep(backoff)
                backoff = min(backoff + 60, 600)

    # --- Get last Bluesky post time ---
    last_bsky = get_last_bsky(client, config["bsky"]["handle"]) if not offline else arrow.get(0)

    # --- Parse feed ---
    feed = fastfeedparser.parse(config["feed"])

    for item in feed.entries:
        rss_time = arrow.get(item.published)
        logging.info("RSS Time: %s", str(rss_time))
        # Use only the plain title as content, and add the link on a new line
        title_text = BeautifulSoup(item.title, "html.parser").get_text().strip()
        post_text = f"{title_text}\n{item.link}"
        logging.info("Title+link used as content: %s", post_text)
        rich_text = make_rich(post_text)
        logging.info("Rich text length: %d" % (len(rich_text.build_text())))
        logging.info("Filtered Content length: %d" % (len(post_text)))
        if rss_time > last_bsky: # Only post if newer than last Bluesky post
        #if True:  # FOR TESTING ONLY! Revert after test.

            link_metadata = fetch_link_metadata(item.link)
            images = []

            # Try to fetch image from snippet (Open Graph/Twitter Card)
            if link_metadata.get("image") and not offline:
                img = get_image_from_url(link_metadata["image"], client)
                if img:
                    images.append(img)

            logging.info("Images length: %d" % (len(images)))

            # --- Add external embed for link preview ---
            external_embed = None
            if link_metadata.get("title") or link_metadata.get("description"):
                external_embed = models.AppBskyEmbedExternal.Main(
                    external=models.AppBskyEmbedExternal.External(
                        uri=item.link,
                        title=link_metadata.get("title") or "Link",
                        description=link_metadata.get("description") or "",
                        thumb=None,
                    )
                )

            # Compose embed (images or link preview)
            embed = None
            if images:
                embed = models.AppBskyEmbedImages.Main(images=images)
            elif external_embed:
                embed = external_embed

            # Post
            try:
                if not offline:
                    client.send_post(rich_text, embed=embed)
                logging.info("Sent post %s" % (item.link))
            except Exception as e:
                logging.exception("Failed to post %s" % (item.link))
        else:
            logging.debug("Not sending %s" % (item.link))

if __name__ == "__main__":
    main()