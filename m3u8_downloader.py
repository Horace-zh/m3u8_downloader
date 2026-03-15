#!/usr/bin/python3
import os
import ssl
import shutil
import asyncio
import aiohttp
import aiofiles
import argparse
import requests
import warnings
from tqdm import tqdm
from urllib.parse import urljoin
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 禁用SSL警告
warnings.filterwarnings('ignore', message='Unverified HTTPS request')
requests.packages.urllib3.disable_warnings()

# 忽略证书验证
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

HEADERS = {"user-agent": "Mozilla/5.0 (Linux; Android 12; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Mobile Safari/537.36"}

class M3U8Downloader:
    def __init__(self, m3u8_url, output_file, max_workers):
        self.m3u8_url     = m3u8_url
        self.output_file  = output_file
        self.max_workers  = max_workers
        self.cache_dir    = "cache_segments"
        self.key_uri      = None
        self.iv_hex       = None
        self.is_encrypted = False
        self.segment_urls = []

    def fetch_m3u8(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        if self.m3u8_url.startswith("http"):
            resp = requests.get(self.m3u8_url, headers=HEADERS, verify=False, timeout=10)
            resp.raise_for_status()
            content = resp.text
            with open(f"{self.cache_dir}/playlist.m3u8", "w", encoding="utf-8") as f:
                f.write(content)
            return content
        else:
            if os.path.exists(self.m3u8_url):
                with open(self.m3u8_url,mode="r",encoding="utf-8") as f:
                    content = f.read()
            return content
        
    def parse_m3u8(self, content):
        lines = content.splitlines()
        base_url = self.m3u8_url.rsplit("/", 1)[0] + "/"
        domain = self.m3u8_url.split("/")[0] + "//" + self.m3u8_url.split("/")[2]

        for line in lines:
            if line.startswith("#EXT-X-KEY"):
                self.is_encrypted = True
                parts = line.split(",")
                for part in parts:
                    if "URI=" in part:
                        key_path = part.split("=")[1].strip('"')
                        if key_path.startswith("http"):
                            self.key_uri = key_path
                        else:
                            self.key_uri = urljoin(base_url, key_path)
                    if "IV=" in part:
                        iv = part.split("=")[1].strip('"')
                        self.iv_hex = iv[2:] if iv.startswith("0x") else iv
                if not self.iv_hex:
                    self.iv_hex = "0" * 32
                break

        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                if line.startswith("http"):
                    self.segment_urls.append(line)
                elif line.startswith("/"):
                    self.segment_urls.append(domain + line)
                else:
                    self.segment_urls.append(urljoin(base_url, line))

    async def download_segment(self, session, sem, url, idx, pbar):
        async with sem:
            for attempt in range(3):
                try:
                    async with session.get(url, ssl=ssl_context) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            async with aiofiles.open(f"{self.cache_dir}/{idx}.ts", "wb") as f:
                                await f.write(data)
                            pbar.update(1)
                            return True
                except Exception:
                    if attempt == 2:
                        return False
                    await asyncio.sleep(1)
        return False

    async def download_all(self):
        connector = aiohttp.TCPConnector(limit=self.max_workers, ssl=ssl_context)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=HEADERS) as session:
            sem = asyncio.Semaphore(self.max_workers)
            with tqdm(total=len(self.segment_urls), desc="下载进度：") as pbar:
                tasks = [self.download_segment(session, sem, url, i, pbar) for i, url in enumerate(self.segment_urls)]
                results = await asyncio.gather(*tasks)
            return sum(results)

    def merge_segments(self):
        seg_files = sorted(
            [f for f in os.listdir(self.cache_dir) if f.endswith(".ts")],
            key=lambda x: int(x.split(".")[0])
        )
        with open(self.output_file, "wb") as out:
            for sf in seg_files:
                with open(f"{self.cache_dir}/{sf}", "rb") as seg:
                    out.write(seg.read())

    def decrypt(self):
        try:
            if self.key_uri.startswith("http"):
                resp = requests.get(self.key_uri, headers=HEADERS, verify=False, timeout=10)
                key_bytes = resp.content
            else:
                key_bytes = bytes.fromhex(self.key_uri) if len(self.key_uri) == 32 else self.key_uri.encode()

            if len(self.iv_hex) == 32:
                iv_bytes = bytes.fromhex(self.iv_hex)
            else:
                iv_bytes = self.iv_hex.encode()[:16] if self.iv_hex else b'\x00'*16
                if len(iv_bytes) < 16:
                    iv_bytes += b'\x00' * (16 - len(iv_bytes))

            if len(key_bytes) not in (16, 24, 32):
                key_bytes = (key_bytes + b'\x00'*32)[:32]

            with open(self.output_file, "rb") as f:
                encrypted = f.read()

            cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(encrypted) + decryptor.finalize()

            try:
                pad_len = decrypted[-1]
                if 1 <= pad_len <= 16:
                    decrypted = decrypted[:-pad_len]
            except:
                pass

            with open(self.output_file, "wb") as f:
                f.write(decrypted)
        except Exception as e:
            print(f"\n解密失败: {e}")
            raise

    def clean_cache(self):
        if os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)

    def run(self):
        try:
            content = self.fetch_m3u8()
            self.parse_m3u8(content)
            print(f"解析到 {len(self.segment_urls)} 个片段，并发数: {self.max_workers}")
            downloaded = asyncio.run(self.download_all())
            print(f"\n下载完成: {downloaded}/{len(self.segment_urls)}")
            if downloaded == len(self.segment_urls):
                self.merge_segments()
                if self.is_encrypted:
                    print("检测到加密，开始解密...")
                    self.decrypt()
                print(f"合并完成: {self.output_file}")
            else:
                print("下载不完整，跳过合并")
        except KeyboardInterrupt:
            print("\n用户中断")
        except Exception as e:
            print(f"\n错误: {e}")
        finally:
            self.clean_cache()
            print("清理缓存完成")

def main():
    parser = argparse.ArgumentParser(description="M3U8 视频下载器")
    parser.add_argument("url", type=str,help="M3U8 文件的 URL, 也可以传入本地M3U8文件路径")  
    parser.add_argument("-o", "--output", default="video.ts", help="输出文件名 (默认: video.ts)")
    parser.add_argument("-c", "--concurrent", type=int, default=10, help="并发下载数 (默认: 10)")
    args = parser.parse_args()

    downloader = M3U8Downloader(args.url, args.output, args.concurrent)
    downloader.run()

if __name__ == "__main__":
    main()
