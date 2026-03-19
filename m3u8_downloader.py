import re
import m3u8
import os, sys
import requests
import argparse
from Crypto.Cipher import AES
import asyncio, aiohttp, aiofiles

def parse_m3u8(source, headers={}):
    try:
        playlist = m3u8.load(source, headers=headers)
    except Exception as e:
        print(f"Failed to parse m3u8: {e}")
        return None, None, None
    key_data, iv_bytes = None, None
    if playlist.segments:
        key = playlist.segments[0].key
        if key and key.method == 'AES-128':
            try:
                resp = requests.get(key.absolute_uri, timeout=30)
                resp.raise_for_status()
                key_data = resp.content
            except Exception as e:
                print(f"Failed to fetch key: {e}")
            if key.iv:
                ivhex = key.iv[2:] if key.iv.startswith('0x') else key.iv
                try:
                    iv_bytes = bytes.fromhex(ivhex)
                except Exception:
                    iv_bytes = None
    return playlist, key_data, iv_bytes

def decrypt_aes128_cbc(data, key, iv):
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.decrypt(data)

async def download_single(idx, url, cache_dir, key=None, iv=None, max_retries=16, retry_delay=2, headers=None):
    part_path = os.path.join(cache_dir, f"{idx}.ts")
    for _ in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        chunk = await resp.read()
                        if key:
                            ivbytes = iv if iv is not None else idx.to_bytes(16, 'big')
                            chunk = decrypt_aes128_cbc(chunk, key, ivbytes)
                        async with aiofiles.open(part_path, "wb") as outf:
                            await outf.write(chunk)
                        return True
        except Exception:
            pass
        await asyncio.sleep(retry_delay)
    return False

async def download_segments(urls, cache_dir, concurrent=16, key=None, iv=None, max_retries=16, headers={}):
    os.makedirs(cache_dir, exist_ok=True)
    sem = asyncio.Semaphore(concurrent)
    async def wrap(idx, url):
        async with sem:
            await download_single(idx, url, cache_dir, key, iv, max_retries, headers=headers)
    await asyncio.gather(*(wrap(idx, url) for idx, url in enumerate(urls)))

def merge_segments(cache_dir, outfile, keep=False):
    files = []
    for fname in os.listdir(cache_dir):
        m = re.match(r"^(\d+)\.ts$", fname)
        if m:
            files.append((int(m.group(1)), fname))
    if not files:
        print("No segments found, merge failed")
        return False
    files.sort()
    with open(outfile, "wb") as outfp:
        for _, fname in files:
            fpath = os.path.join(cache_dir, fname)
            with open(fpath, "rb") as infp:
                outfp.write(infp.read())
            if not keep:
                os.remove(fpath)
    if not keep:
        try: os.rmdir(cache_dir)
        except: pass
    return True

def main():
    parser = argparse.ArgumentParser(description="m3u8 Downloader")
    parser.add_argument("source", help="m3u8 URL or file")
    parser.add_argument("-o", "--output", default="output.mp4", help="Output file")
    parser.add_argument("-c", "--concurrent", type=int, default=10, help="Concurrent downloads")
    args = parser.parse_args()
    # 解析M3U8
    playlist, key, iv = parse_m3u8(args.source)
    if not playlist or not playlist.segments:
        print("Invalid m3u8 or no segments found")
        sys.exit(2)
    urls = [seg.absolute_uri for seg in playlist.segments]
    cache_dir = os.path.join(os.path.dirname(args.output), ".cache_" + os.path.basename(args.output))
    # 下载视频分片
    asyncio.run(
        download_segments(
            urls,
            cache_dir,
            concurrent=args.concurrent,
            key=key,
            iv=iv,
        )
    )
    # 合并视频分片
    merged = merge_segments(cache_dir, args.output)
    if not merged:
        print("Merge failed")
        sys.exit(3)
    print("Done !")

if __name__ == "__main__":
    
    main()
