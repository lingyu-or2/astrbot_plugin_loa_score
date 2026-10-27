"""图片获取与校验（ adapted from astrbot_plugin_noooob 的成熟实现）。

aiohttp 缺失时仅联网下载不可用；validate_image 等纯函数不受影响，便于离线自测。
"""

import asyncio
import base64
from pathlib import Path

try:
    import aiohttp
except ImportError:  # 本地离线自测环境可能没有 aiohttp
    aiohttp = None

# 视觉模型侧限制宽松，但群图链本身有限制，取 10MB
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def validate_image(image_data: bytes):
    """校验图片格式与大小。返回 (是否有效, 错误信息, 扩展名)。"""
    if len(image_data) > MAX_IMAGE_BYTES:
        return False, (
            f'图片大小超过 10MB 限制（当前 {len(image_data)/1024/1024:.2f}MB），'
            '请压缩或裁剪后重试'), ''
    if image_data[:4] == b'\x89PNG':
        return True, '', 'png'
    if image_data[:3] == b'\xff\xd8\xff':
        return True, '', 'jpg'
    if image_data[:4] == b'RIFF' and image_data[8:12] == b'WEBP':
        return True, '', 'webp'
    if image_data[:3] == b'GIF':
        return False, '不支持 GIF 格式，请上传 PNG 或 JPG 图片', ''
    if image_data[:2] == b'BM':
        return False, '不支持 BMP 格式，请上传 PNG 或 JPG 图片', ''
    return False, '未知的图片格式，请上传 PNG 或 JPG 图片', ''


async def acquire_image_bytes(image_comp):
    """从 Image 消息组件获取图片字节。

    优先 AstrBot 内置媒体解析（支持 http/base64/本地路径），
    失败回退自研下载（兼容需特定 Referer 的 QQ 多媒体图链）。
    返回 (bytes 或 None, 错误信息)。
    """
    image_data = None
    try:
        image_path = await image_comp.convert_to_file_path()
        image_data = await asyncio.to_thread(Path(image_path).read_bytes)
    except Exception as e:
        if aiohttp is None:
            return None, f'图片读取失败且缺少 aiohttp 无法回退下载: {e}'

    if not image_data:
        image_url = getattr(image_comp, 'url', None) or ''
        if (not image_url and isinstance(getattr(image_comp, 'file', None), str)
                and image_comp.file.startswith('http')):
            image_url = image_comp.file
        if image_url:
            image_data = await download_image(image_url)
        if not image_data:
            return None, '无法获取图片：链接可能已过期或网络异常。请重新发送图片后再试。'
    return image_data, ''


async def download_image(url: str):
    """下载图片（支持 data:image base64 与 http(s) 链接）。

    针对 QQ 多媒体图链带 Referer，带 3 次递增超时重试；4xx 不重试。
    返回图片字节或 None。
    """
    if not url:
        return None

    if url.startswith('data:image'):
        try:
            return base64.b64decode(url.split(',', 1)[1])
        except Exception:
            return None

    if aiohttp is None:
        return None

    max_retries = 3
    retry_delays = [2, 5, 10]
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36'),
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Referer': 'https://multimedia.nt.qq.com.cn/',
        'Connection': 'keep-alive',
    }

    for attempt in range(max_retries):
        connector = aiohttp.TCPConnector(ssl=False, force_close=True)
        timeout = aiohttp.ClientTimeout(
            total=45 + attempt * 15,
            connect=15 + attempt * 10,
            sock_read=30,
        )
        try:
            async with aiohttp.ClientSession(
                    connector=connector, timeout=timeout) as session:
                async with session.get(url, headers=headers,
                                       allow_redirects=True) as response:
                    if response.status == 200:
                        data = await response.read()
                        if data:
                            return data
                        continue
                    if 400 <= response.status < 500:
                        return None  # 客户端错误（链接失效等），重试无意义
        except (asyncio.TimeoutError, aiohttp.ClientError, Exception):
            pass
        if attempt < max_retries - 1:
            await asyncio.sleep(retry_delays[attempt])

    return None
