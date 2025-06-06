import argparse
import arrow
import fastfeedparser
import logging
import re
import httpx
import time
from atproto import Client, client_utils, models  # Ensure you have the correct library installed
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# --- Logging ---
LOG_PATH = "rss2bsky.log"
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
        
        # If an image URL is found, append "_full.jpg" to the base URL
        image_url = None
        if image and image.has_attr("content"):
            base_image_url = image["content"]
            # Append "_full.jpg" only if not already present
            if not base_image_url.endswith("_full.jpg"):
                parsed_url = urlparse(base_image_url)
                base_path, ext = parsed_url.path.rsplit('.', 1)
                image_url = f"{parsed_url.scheme}://{parsed_url.netloc}{base_path}_full.jpg"
            else:
                image_url = base_image_url

        return {
            "title": title["content"] if title and title.has_attr("content") else (title.text if title else ""),
            "description": desc["content"] if desc and desc.has_attr("content") else "",
            "image": image_url,
        }
    except Exception as e:
        logging.warning(f"Could not fetch link metadata for {url}: {e}")
        return {}

def get_last_bsky(client, handle):
    try:
        timeline = client.get_author_feed(handle)
        for titem in timeline.feed:
            # Only care about top-level, non-reply posts
            if titem.reason is None and getattr(titem.post.record, "reply", None) is None:
                logging.info("Record created %s", str(titem.post.record.created_at))
                return arrow.get(titem.post.record.created_at)
        return arrow.get(0)
    except Exception as e:
        logging.warning(f"Failed to get last Bluesky post: {e}")
        return arrow.get(0)

def get_image_from_url(image_url, client, alt_text="Preview image"):
    try:
        r = httpx.get(image_url)
        if r.status_code != 200:
            logging.warning(f"Failed to fetch image from {image_url}, status code: {r.status_code}")
            return None
        img_blob = client.upload_blob(r.content)
        img_model = models.AppBskyEmbedImages.Image(
            alt=alt_text, image=img_blob.blob
        )
        return img_model
    except Exception as e:
        logging.warning(f"Could not fetch/upload image from {image_url}: {e}")
        return None

def is_html(text):
    return bool(re.search(r'<.*?>', text))

def make_rich(content):
    text_builder = client_utils.TextBuilder()
    lines = content.split("\n")
    for line in lines:
        # If the line is a URL, make it a clickable link
        if line.startswith("http"):
            url = line.strip()
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

def extract_image_from_description(description):
    try:
        # Parse the description HTML
        soup = BeautifulSoup(description, "html.parser")
        img_tag = soup.find("img")
        if img_tag and img_tag.has_attr("src"):
            # Get the image URL and replace "_thumb" with "_full"
            img_url = img_tag["src"]
            img_url_full = img_url.replace("_thumb", "_full")
            return img_url_full
        return None
    except Exception as e:
        logging.warning(f"Failed to extract image from description: {e}")
        return None

def main():
    # --- Parse command-line arguments ---
    parser = argparse.ArgumentParser(description="Post RSS to Bluesky.")
    parser.add_argument("rss_feed", help="RSS feed URL")
    parser.add_argument("bsky_handle", help="Bluesky handle")
    parser.add_argument("bsky_username", help="Bluesky username")
    parser.add_argument("bsky_app_password", help="Bluesky app password")
    args = parser.parse_args()
    feed_url = args.rss_feed
    bsky_handle = args.bsky_handle
    bsky_username = args.bsky_username
    bsky_password = args.bsky_app_password

    # --- Login ---
    client = Client()
    backoff = 60
    while True:
        try:
            client.login(bsky_username, bsky_password)
            break
        except Exception as e:
            logging.exception("Login exception")
            time.sleep(backoff)
            backoff = min(backoff + 60, 600)

    # --- Get last Bluesky post time ---
    last_bsky = get_last_bsky(client, bsky_handle)

    # --- Parse feed ---
    feed = fastfeedparser.parse(feed_url)

    for item in feed.entries:
        rss_time = arrow.get(item.published)
        logging.info("RSS Time: %s", str(rss_time))
        # Use only the plain title as content, and add the link on a new line
        if is_html(item.title):
            title_text = BeautifulSoup(item.title, "html.parser").get_text().strip()
        else:
            title_text = item.title.strip()
        post_text = f"{title_text}\n{item.link}"
        logging.info("Title+link used as content: %s", post_text)
        rich_text = make_rich(post_text)
        logging.info("Rich text length: %d" % (len(rich_text.build_text())))
        logging.info("Filtered Content length: %d" % (len(post_text)))
        if rss_time > last_bsky:  # Only post if newer than last Bluesky post
        #if True:  # Always post, remove this line to enable posting condition
            images = []

            # Extract image from <description> and replace "_thumb" with "_full"
            if item.description:
                img_url_full = extract_image_from_description(item.description)
                if img_url_full:
                    alt_text = title_text or "Preview image"
                    img = get_image_from_url(img_url_full, client, alt_text=alt_text)
                    if img:
                        images.append(img)

            logging.info("Images length: %d" % (len(images)))

            # --- Add external embed for link preview ---
            external_embed = None
            if item.title or item.description:
                external_embed = models.AppBskyEmbedExternal.Main(
                    external=models.AppBskyEmbedExternal.External(
                        uri=item.link,
                        title=item.title or "Link",
                        description=item.description or "",
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
                client.send_post(rich_text, embed=embed)
                logging.info("Sent post %s" % (item.link))
            except Exception as e:
                logging.exception("Failed to post %s" % (item.link))
        else:
            logging.debug("Not sending %s" % (item.link))

if __name__ == "__main__":
    main()