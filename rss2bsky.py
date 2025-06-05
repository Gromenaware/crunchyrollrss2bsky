import argparse
import fastfeedparser
import logging
import httpx
import time
from atproto import Client, client_utils, models
from bs4 import BeautifulSoup

LOG_PATH = "crunchyroll2bsky.log"
logging.basicConfig(
    format="%(asctime)s %(message)s",
    filename=LOG_PATH,
    encoding="utf-8",
    level=logging.INFO,
)

def make_rich(content, link):
    text_builder = client_utils.TextBuilder()
    text_builder.text(content + "\n")
    text_builder.link(link, link)
    return text_builder

def get_full_jpg(item):
    # Prefer media:thumbnail with _full.jpg
    if hasattr(item, 'media_thumbnail') and item.media_thumbnail:
        for thumb in item.media_thumbnail:
            url = thumb.get('url')
            if url and url.endswith('_full.jpg'):
                return url
    # Fallback: look for any image in content
    if hasattr(item, 'content') and item.content:
        for entry in item.content:
            soup = BeautifulSoup(entry.value, "html.parser")
            img = soup.find('img')
            if img and img.get('src', '').endswith('_full.jpg'):
                return img['src']
    return None

def get_permalink(item):
    if hasattr(item, "id") and isinstance(item.id, str):
        return item.id
    if hasattr(item, "guid") and isinstance(item.guid, str):
        return item.guid
    if hasattr(item, "link") and isinstance(item.link, str):
        return item.link
    return None

def main():
    parser = argparse.ArgumentParser(description="Post Crunchyroll RSS to Bluesky.")
    parser.add_argument("bsky_username", help="Bluesky username (handle, e.g. user.bsky.social)")
    parser.add_argument("bsky_app_password", help="Bluesky app password")
    args = parser.parse_args()
    feed_url = "https://feeds.feedburner.com/crunchyroll/rss/anime?lang=esES"
    bsky_username = args.bsky_username
    bsky_password = args.bsky_app_password

    client = Client()
    backoff = 60
    while True:
        try:
            client.login(bsky_username, bsky_password)
            print("Logged into Bluesky!")
            break
        except Exception as e:
            logging.exception("Login exception")
            print("Login failed, retrying in", backoff, "seconds...")
            time.sleep(backoff)
            backoff = min(backoff + 60, 600)

    feed = fastfeedparser.parse(feed_url)
    print(f"Feed contains {len(feed.entries)} entries")
    logging.info(f"Feed contains {len(feed.entries)} entries")

    for item in feed.entries:
        permalink = get_permalink(item)
        if not permalink:
            logging.warning(f"Skipping item with title: {getattr(item, 'title', 'NO TITLE')} -- no permalink found")
            continue

        title_text = BeautifulSoup(item.title, "html.parser").get_text().strip()
        rich_text = make_rich(title_text, permalink)
        print(f"DEBUG: Post text: {rich_text.build_text()}")

        image_url = get_full_jpg(item)
        print(f"DEBUG: Image URL: {image_url}")

        embed = None
        if image_url:
            try:
                r = httpx.get(image_url)
                if r.status_code == 200:
                    img_blob = client.upload_blob(r.content).blob
                    image_model = models.AppBskyEmbedImages.Image(
                        alt=title_text or "Preview image", image=img_blob
                    )
                    embed = models.AppBskyEmbedImages.Main(images=[image_model])
                    print(f"DEBUG: Image attached")
                else:
                    print(f"DEBUG: Failed to fetch image: {r.status_code}")
            except Exception as e:
                print(f"DEBUG: Could not fetch/upload image from {image_url}: {e}")

        try:
            print(f"Posting: {title_text}\n{permalink}")
            resp = client.send_post(
                rich_text.build_text(),
                facets=rich_text.facets if hasattr(rich_text, 'facets') else None,
                embed=embed
            )
            print("Posted:", resp['uri'])
            logging.info("Sent post %s" % (permalink))
        except Exception as e:
            print(f"Failed to post {permalink}: {e}")
            logging.exception("Failed to post %s" % (permalink))

if __name__ == "__main__":
    main()