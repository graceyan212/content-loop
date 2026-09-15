#!/usr/bin/env python3
"""
upload.py — put a local image into Metricool's own media store and get a URL back.

WHY NOT OUR OWN S3: Metricool attaches media by public URL only. Serving from our
bucket means either making a bucket path world-readable (a security footgun, and
correctly refused) or minting presigned URLs whose signatures die with the broker's
temporary session token. Metricool exposes its own upload transaction flow instead,
so the file goes straight to their store and we never host anything.

Three steps, all discovered from their live swagger:

  1. PUT  /v2/media/s3/upload-transactions   -> presigned target(s)
       body: {resourceType:"planner", contentType, fileExtension, parts:[...]}
       each part: {size, startByte, endByte, hash}   (hash = md5 of that slice)
       returns: {key, bucket, fileUrl, presignedUrl | parts[], uploadType, ...}

  2. PUT the bytes to presignedUrl (single) or each parts[].presignedUrl (multi)

  3. PUT  /v2/media  (mergeUploadedChunks)    -> final hosted url
       body: {chunkOptions:{chunkUrls, hash}, mergedFileName, folder}
       Only needed for a multipart upload; single-part is already complete, and
       we confirm it with GET /v2/media/s3/upload-transactions?key=&bucket=

    python upload.py path/to/image.jpg
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metricool as mc


def _sha256_b64(blob: bytes) -> str:
    """S3 additional-checksum form: base64 of the raw SHA-256 digest."""
    return base64.b64encode(hashlib.sha256(blob).digest()).decode()


def _put_bytes(url: str, blob: bytes, content_type: str) -> int:
    req = urllib.request.Request(url, data=blob, method="PUT")
    req.add_header("Content-Type", content_type)
    # The presigned URL signs content-length;content-type;host;x-amz-checksum-sha256.
    # Omit the checksum header and S3 returns 403 SignatureDoesNotMatch with no hint
    # about which header is missing - this cost a debugging round.
    req.add_header("x-amz-checksum-sha256", _sha256_b64(blob))
    req.add_header("User-Agent", mc.UA)
    with urllib.request.urlopen(req, context=mc._SSL, timeout=180) as r:
        return r.status


# TikTok photo posts REJECT PNG at publish time, not at upload time: Metricool
# accepts the media, schedules the post, and only fails when it hands off to
# TikTok — "The 'image/png' type is not allowed, use 'image/jpeg' or 'image/webp'".
# So the check belongs here, before a post is built around a doomed file. This
# cost one wasted scheduled slot.
TIKTOK_PHOTO_OK = (".jpg", ".jpeg", ".webp")

# Reels are a VIDEO placement, so the photo guard above must not apply to them — but
# it also must not simply be switched off, or the PNG-at-publish-time failure it was
# written to catch comes straight back. Video gets its own allow-list instead.
VIDEO_OK = (".mp4", ".mov")


def upload(path: str, verbose: bool = True, for_tiktok_photo: bool = True) -> str:
    low = path.lower()
    if low.endswith(VIDEO_OK):
        for_tiktok_photo = False          # a reel, not a TikTok photo
    elif for_tiktok_photo and not low.endswith(TIKTOK_PHOTO_OK):
        alt = os.path.splitext(path)[0] + ".jpg"
        hint = (f" A sibling {os.path.basename(alt)} already exists — use that."
                if os.path.exists(alt) else "")
        raise SystemExit(
            f"REFUSING TO UPLOAD {os.path.basename(path)}: TikTok photo posts accept only "
            f"{', '.join(TIKTOK_PHOTO_OK)}. PNG is accepted by Metricool and then fails at "
            f"publish time, wasting the scheduled slot.{hint}")
    creds = mc.load_creds()
    blob = open(path, "rb").read()
    ext = os.path.splitext(path)[1].lstrip(".").lower() or "jpg"
    ctype = mimetypes.guess_type(path)[0] or "image/jpeg"
    # `hash` in the part descriptor must be the base64 SHA-256, NOT an md5 hex
    # digest - the server signs the presigned URL against exactly this value.
    digest = _sha256_b64(blob)

    # 1. ask for a transaction. One part: these are ~0.5MB images, well under any
    #    multipart threshold, but we honour whatever uploadType comes back.
    body = {
        "resourceType": "planner",
        "contentType": ctype,
        "fileExtension": ext,
        # endByte is EXCLUSIVE - the API computes size as endByte-startByte and
        # rejected len(blob)-1 with a one-byte mismatch.
        "parts": [{"size": len(blob), "startByte": 0,
                   "endByte": len(blob), "hash": digest}],
    }
    status, resp = mc._req("PUT", "/v2/media/s3/upload-transactions", creds, body=body)
    if status != 200:
        raise SystemExit(f"transaction failed {status}: {json.dumps(resp)[:400]}")
    data = (resp or {}).get("data") or resp
    if verbose:
        print(f"  transaction: uploadType={data.get('uploadType')} key={data.get('key')}")

    # 2. push the bytes
    parts = data.get("parts") or []
    if data.get("presignedUrl"):
        code = _put_bytes(data["presignedUrl"], blob, ctype)
        if verbose:
            print(f"  uploaded single part -> {code}")
    elif parts:
        for p in parts:
            s, e = p.get("startByte", 0), p.get("endByte", len(blob))
            code = _put_bytes(p["presignedUrl"], blob[s:e], ctype)   # endByte exclusive
            if verbose:
                print(f"  part {p.get('partNumber')} -> {code}")
    else:
        raise SystemExit(f"no presigned target in response: {json.dumps(data)[:400]}")

    # 3. confirm it landed, and merge if this was multipart
    if parts and not data.get("presignedUrl"):
        merge = {"chunkOptions": {"chunkUrls": [p["presignedUrl"] for p in parts],
                                  "hash": digest},
                 "mergedFileName": os.path.basename(path)}
        status, resp = mc._req("PUT", "/v2/media", creds, body=merge)
        if status != 200:
            raise SystemExit(f"merge failed {status}: {json.dumps(resp)[:400]}")
        url = ((resp or {}).get("data") or {}).get("url") or data.get("fileUrl")
    else:
        url = data.get("fileUrl")
        status, info = mc._req("GET", "/v2/media/s3/upload-transactions", creds,
                               params={"key": data.get("key"), "bucket": data.get("bucket")})
        if status == 200:
            d = (info or {}).get("data") or {}
            if verbose:
                print(f"  confirmed: {d.get('size')} bytes, {d.get('contentType')}")
            url = d.get("fileUrl") or url

    if not url:
        raise SystemExit("upload succeeded but no fileUrl was returned")
    return url


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    a = ap.parse_args(argv)
    url = upload(a.path)
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
