import re 
import m3u8
import os, sys
import requests
import argparse
from Crypto.Cipher import AES
from datetime import datetime
import asyncio, aiohttp, aiofiles
from rich.console  import Console
from rich.progress import Progress, BarColumn, TaskProgressColumn, TransferSpeedColumn, TimeRemainingColumn, TextColumn

console = Console()

headers = {}
headers_async = {"user-agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"}

def log(msg, level="info"):
    time_str = datetime.now().strftime("%H:%M:%S")
    if level == "ok":
        console.print(f"[green][{time_str}] {msg}[/green]")
    elif level == "warn":
        console.print(f"[yellow][{time_str}] {msg}[/yellow]")
    elif level == "fail":
        console.print(f"[red][{time_str}] {msg}[/red]")
    else:
        console.print(f"[dim][{time_str}][/dim] {msg}")

def parse_m3u8(source):
    try:
        playlist = m3u8.load(source)
        log("m3u8 解析成功", "ok")
    except Exception as e:
        log(f"解析失败: {e}", "fail")
        return None, None, None
    key_data, iv_bytes = None, None
    if playlist.segments:
        key = playlist.segments[0].key
        if key and key.method == 'AES-128':
            try:
                resp = requests.get(key.absolute_uri, timeout=30)
                resp.raise_for_status()
                key_data = resp.content
                log("成功获取加密 key")
            except Exception as e:
                log(f"key 下载失败: {e}", "fail")
            if key.iv:
                ivhex = key.iv[2:] if key.iv.startswith('0x') else key.iv
                try:
                    iv_bytes = bytes.fromhex(ivhex)
                except Exception:
                    log(f"IV 格式错误: {key.iv}", "warn")
                    iv_bytes = None
    return playlist, key_data, iv_bytes

def decrypt_aes128_cbc(data, key, iv):
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.decrypt(data)

async def download_single(idx, url, cache_dir, progress_task, progress, total, key=None, iv=None, max_retries=16, retry_delay=2):
    part_path = os.path.join(cache_dir, f"{idx}.ts")
    for retry in range(1, max_retries+1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers= headers_async) as resp:
                    if resp.status == 200:
                        chunk = await resp.read()
                        if key:
                            ivbytes = iv if iv is not None else idx.to_bytes(16, 'big')
                            chunk = decrypt_aes128_cbc(chunk, key, ivbytes)
                        async with aiofiles.open(part_path, "wb") as outf:
                            await outf.write(chunk)
                        progress.update(progress_task, advance=1)
                        log(f"index {idx+1}/{total} OK！", "ok")
                        return
                    else:
                        log(f"分片 {idx+1}/{total} HTTP{resp.status}, 重试 {retry}/{max_retries}", "warn")
        except Exception as e:
            log(f"分片 {idx+1}/{total} 网络异常：{e}，重试 {retry}/{max_retries}", "warn")
        await asyncio.sleep(retry_delay)
    log(f"分片 {idx+1}/{total} 达到最大重试次数，放弃", "fail")
    progress.update(progress_task, advance=1)

async def download_segments(urls, cache_dir, concurrent=16, key=None, iv=None, max_retries=16):
    os.makedirs(cache_dir, exist_ok=True)
    total = len(urls)
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("[cyan]进度:[/cyan]", total=total)
        sem = asyncio.Semaphore(concurrent)
        async def wrap(idx, url):
            async with sem:
                await download_single(idx, url, cache_dir, task, progress, total, key, iv, max_retries)
        await asyncio.gather(*(wrap(idx, url) for idx, url in enumerate(urls)))
    log(f"全部分片下载完毕: {total}/{total}", "ok")

def merge_segments(cache_dir, outfile, keep=False):
    files = []
    for fname in os.listdir(cache_dir):
        m = re.match(r"^(\d+)\.ts$", fname)
        if m:
            files.append((int(m.group(1)), fname))
    if not files:
        log("未找到任何分片，合并失败", "fail")
        return False
    files.sort()
    with open(outfile, "wb") as outfp:
        total = len(files)
        task = console.status("[cyan]正在合并分片...[/cyan]", spinner="dots")
        with task:
            for i, (_, fname) in enumerate(files):
                fpath = os.path.join(cache_dir, fname)
                with open(fpath, "rb") as infp:
                    outfp.write(infp.read())
                if not keep:
                    os.remove(fpath)
    log(f"合并完毕: {outfile}", "ok")
    if not keep:
        try: os.rmdir(cache_dir)
        except Exception: pass
    return True

def main():
    parser = argparse.ArgumentParser(description="m3u8 Downloader")
    parser.add_argument("source", help="m3u8文件URL或本地路径")
    parser.add_argument("-o", "--output", default="output.mp4",     help="输出文件路径" )
    parser.add_argument("-c", "--concurrent", type=int, default=10, help="并发下载数"   )
    parser.add_argument("-r", "--retries",    type=int, default=16, help="单分片最大重试")
    parser.add_argument("-k", "--keep", action="store_true",        help="合并后保留分片")
    args = parser.parse_args()

    log("🎬 M3U8下载器")
    playlist, key, iv = parse_m3u8(args.source)
    if not playlist or not playlist.segments:
        log("m3u8无效或未找到分片", "fail")
        sys.exit(2)
    urls = [seg.absolute_uri for seg in playlist.segments]
    log(f"一共 {len(urls)} 个分片")
    cache_dir = os.path.join(os.path.dirname(args.output), ".cache_" + os.path.basename(args.output))

    asyncio.run(
        download_segments(
            urls,
            cache_dir,
            concurrent=args.concurrent,
            key=key,
            iv=iv,
            max_retries=args.retries
        )
    )
    
    merged = merge_segments(cache_dir, args.output, keep=args.keep)
    if not merged:
        log("合并失败", "fail")
        sys.exit(3)
    log("全部完成，Enjoy!", "ok")

if __name__ == "__main__":
    main()
