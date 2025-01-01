import requests
from collections import defaultdict
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import time
import asyncio
import aiohttp
from tqdm import tqdm
import argparse
import socket
import ipaddress
import warnings
from urllib3.exceptions import InsecureRequestWarning
from pypinyin import lazy_pinyin

# 在文件顶部定义provinces列表
PROVINCES = [
    "广东", "浙江", "北京", "上海", "江苏", "湖南", "山东", 
    "河南", "河北", "安徽", "福建", "重庆", "四川", "贵州", 
    "云南", "陕西", "甘肃", "青海", "内蒙古", "宁夏", "新疆",
    "西藏", "黑龙江", "吉林", "辽宁"
]

# 全局配置
CONFIG = {
    'ENABLE_TEST': False ,  # 设置为 True 开启测试，False 关闭测试
    'TIMEOUT': 5,  # 测试超时时间(秒)
    'MAX_CONCURRENT': 50,  # 最大并发数
    'BATCH_SIZE': 200,  # 批处理大小
    'MAX_RETRIES': 2  # 最大重试次数
}

def is_valid_chinese_text(text):
    """检查文本是否包含有效的中文字符，不含乱码"""
    try:
        # 检查是否能正确解码
        text.encode('utf-8').decode('utf-8')
        
        # 检查包含中文字符
        if any('\u4e00' <= char <= '\u9fff' for char in text):
            # 检查是否包含常见乱码字符
            invalid_chars = {'å', 'é', '¿', '»', '¼', 'è', 'æ', 'ä¸', 'ç', 'å'}
            if any(char in text for char in invalid_chars):
                return False
            return True
        return False
    except UnicodeError:
        return False

def standardize_channel_name(line):
    """标准化频道名称和URL"""
    try:
        # 分割频道名和URL
        parts = line.split(',', 1)
        if len(parts) != 2:
            return None
            
        channel_name = parts[0].strip()
        url = parts[1].strip()
        
        # 检查频道名和URL是否为空
        if not channel_name or not url:
            return None
            
        # 移除URL中的多余参数
        if '$' in url:
            url = url.split('$')[0]
            
        return f"{channel_name},{url}"
    except:
        return None

def is_valid_channel(channel_line):
    """检查频道名称是否有效（不含乱码）"""
    try:
        # 分割频道名和URL
        parts = channel_line.split(',', 1)
        if len(parts) != 2:
            return False
            
        channel_name = parts[0].strip()
        
        # 检查是否包含常见乱码特征
        if '?' in channel_name or 'ã' in channel_name or 'â' in channel_name:
            return False
            
        # 检查是否全是ASCII字符或有效的中文字符
        for char in channel_name:
            if not (0x0020 <= ord(char) <= 0x007E or 0x4E00 <= ord(char) <= 0x9FFF):
                return False
                
        return True
    except:
        return False

def categorize_channel(line):
    """根据频道名称对频道进行分类"""
    if not is_valid_channel(line):
        return None, None
        
    standardized_channel = standardize_channel_name(line)
    if not standardized_channel:
        return None, None
        
    parts = standardized_channel.split(',', 1)
    if len(parts) != 2:
        return None, None
    
    channel_name = parts[0]
    
    # 检查是否是央视频道
    if any(keyword in channel_name for keyword in [
        "CCTV", "央视", "中央电视台", "CETV", "CGTN", "中国教育", "央广"
    ]):
        return "央视频道", standardized_channel
    
    # 检查是否是卫视频道
    if "卫视" in channel_name:
        return "卫视频道", standardized_channel
    
    # 省份和地级市对应关系
    province_city_map = {
        "浙江": ["杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州", "舟山", "台州", "丽水"],
        "广东": ["广州", "深圳", "珠海", "汕头", "佛山", "韶关", "湛江", "肇庆", "江门", "茂名", "惠州"],
        "江苏": ["南京", "苏州", "无锡", "常州", "南通", "扬州", "镇江", "泰州", "盐城", "连云港", "徐州"],
        "四川": ["成都", "绵阳", "德阳", "遂宁", "乐山", "南充", "宜宾", "自贡", "泸州", "达州", "内江"],
        "山东": ["济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁", "泰安", "威海", "日照"],
        "河南": ["郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作", "濮阳", "许昌", "漯河"],
        "河北": ["石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口", "承德", "沧州", "廊坊", "衡水"],
        "湖南": ["长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界", "益阳", "郴州", "永州"],
        "安徽": ["合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆", "黄山", "滁州", "阜阳"],
        "福建": ["福州", "厦门", "莆田", "三明", "泉州", "漳州", "南平", "龙岩", "宁德"],
        "江西": ["南昌", "景德镇", "萍乡", "九江", "新余", "鹰潭", "赣州", "吉安", "宜春", "抚州", "上饶"],
        "云南": ["昆明", "曲靖", "玉溪", "保山", "昭通", "丽江", "普洱", "临沧"],
        "贵州": ["贵阳", "六盘水", "遵义", "安顺", "毕节", "铜仁"],
        "广西": ["南宁", "柳州", "桂林", "梧州", "北海", "防城港", "钦州", "贵港", "玉林"],
        "陕西": ["西安", "铜川", "宝鸡", "咸阳", "渭南", "延安", "汉中", "榆林", "安康", "商洛"],
        "甘肃": ["兰州", "嘉峪关", "金昌", "白银", "天水", "武威", "张掖", "平凉", "酒泉", "庆阳"],
        "青海": ["西宁", "海东", "海北", "黄南", "海南", "果洛", "玉树", "海西"],
        "黑龙江": ["哈尔滨", "齐齐哈尔", "鸡西", "鹤岗", "双鸭山", "大庆", "伊春", "佳木斯", "七台河", "牡丹江", "黑河", "绥化"],
        "吉林": ["长春", "吉林", "四平", "辽源", "通化", "白山", "松原", "白城", "延边"],
        "辽宁": ["沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "阜新", "辽阳", "盘锦", "铁岭"]
    }
    
    # 检查省级和地级市频道
    for province, cities in province_city_map.items():
        if province in channel_name:
            return f"{province}频道", standardized_channel
        # 检查地级市，归类到对应省份
        if any(city in channel_name for city in cities):
            return f"{province}频道", standardized_channel
    
    # 检查特殊频道类型
    if any(keyword in channel_name for keyword in ["体育", "足球", "篮球", "NBA"]):
        return "体育频道", standardized_channel
    
    if any(keyword in channel_name for keyword in ["影视", "电影", "剧场", "电视剧"]):
        return "影视频道", standardized_channel
    
    if any(keyword in channel_name for keyword in ["少儿", "动画", "卡通"]):
        return "少儿频道", standardized_channel
    
    if any(keyword in channel_name for keyword in ["新闻", "资讯"]):
        return "新闻频道", standardized_channel
    
    # 其他频道
    return "其他频道", standardized_channel

def standardize_category_name(category):
    """标准化分类名称"""
    # 跳过包含乱码的分类名称
    if not is_valid_chinese_text(category):
        return "其他频道"
    
    # 移除特殊字符和额外的描述
    category = re.sub(r'[•·]', '', category)
    category = re.sub(r'「[^」]*」', '', category)
    category = re.sub(r'\([^\)]*\)', '', category)
    
    # 标准化分类名称映射
    category_mapping = {
        '央视': '央视频道',
        '卫视': '卫视频道',
        '港澳台': '港澳台频道',
        '体育': '体育频道',
        '影视': '影视频道',
        '电影': '影视频道',
        '少儿': '少儿频道',
        '新闻': '新闻频道',
        '纪录': '纪录频道',
        '音乐': '音乐频道',
        '其他': '其他频道'
    }
    
    # 处理省份频道的情况
    if any(province in category for province in PROVINCES):
        for province in PROVINCES:
            if province in category:
                return f"{province}频道"
    
    # 使用映射转换标准名称
    for key, value in category_mapping.items():
        if key in category:
            return value
    
    return category.strip()

def parse_m3u(content):
    """解析M3U格式内容并转换为txt格式"""
    channels = []
    current_name = None
    
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
            
        if line.startswith('#EXTINF:'):
            # 提取频道名称
            try:
                # 处理带有tvg-name的情况
                if 'tvg-name="' in line:
                    current_name = re.search('tvg-name="([^"]+)"', line).group(1)
                else:
                    # 提取逗号后的名称
                    current_name = line.split(',', 1)[1].strip()
            except:
                current_name = None
        elif not line.startswith('#') and current_name:
            # 这是URL行
            channels.append(f"{current_name},{line}")
            current_name = None
            
    return '\n'.join(channels)

def is_ipv6_address(host):
    """检查是否是IPv6地址"""
    try:
        addr = ipaddress.ip_address(host)
        return addr.version == 6
    except ValueError:
        return False

def is_ipv6_supported():
    """检查系统是否支持IPv6"""
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        return True
    except socket.error:
        return False

async def async_check_url(url, timeout=3, max_retries=1):
    """异步检查URL是否有效"""
    for retry in range(max_retries + 1):
        try:
            # 基本URL格式检查
            parsed = urlparse(url)
            if not all([parsed.scheme, parsed.netloc]):
                return False
            
            # 检查URL协议    
            if parsed.scheme not in ['http', 'https', 'rtmp', 'rtsp']:
                return False
                
            # 检查是否是IPv6地址
            host = parsed.hostname
            is_ipv6 = is_ipv6_address(host) if host else False
            
            # 如果是IPv6地址但系统不支持IPv6，则跳过
            if is_ipv6 and not is_ipv6_supported():
                print(f"跳过IPv6地址 {url} (系统不支持IPv6)")
                return False
                
            connector = aiohttp.TCPConnector(
                force_close=True,
                enable_cleanup_closed=True,
                limit=0,
                ssl=False  # 使用 ssl=False 替代 verify_ssl
            )
            
            async with aiohttp.ClientSession(connector=connector) as session:
                try:
                    timeout_obj = aiohttp.ClientTimeout(
                        total=timeout,
                        connect=2,
                        sock_connect=2,
                        sock_read=timeout
                    )
                    
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Accept': '*/*',
                        'Connection': 'keep-alive'
                    }
                    
                    async with session.get(
                        url,
                        timeout=timeout_obj,
                        headers=headers,
                        allow_redirects=True
                    ) as response:
                        if response.status == 200:
                            content_type = response.headers.get('Content-Type', '').lower()
                            return any(t in content_type for t in [
                                'video/',
                                'application/vnd.apple.mpegurl',
                                'application/x-mpegurl',
                                'application/octet-stream'
                            ])
                except:
                    pass
                    
            return False
                    
        except Exception as e:
            if retry == max_retries:
                return False
            await asyncio.sleep(0.5)
            continue
    return False

async def async_check_stream_url(channel_line):
    """异步检查直播源是否有效"""
    parts = channel_line.split(',', 1)
    if len(parts) != 2:
        return None
        
    channel_name, url = parts
    url = url.strip()
    
    if await async_check_url(url):
        return channel_line
    return None

async def process_channel_batch_async(channels, max_concurrent=None):
    """异步批量处理频道检查"""
    max_concurrent = max_concurrent or CONFIG['MAX_CONCURRENT']
    batch_size = CONFIG['BATCH_SIZE']
    
    valid_channels = set()
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def bounded_check(channel):
        async with semaphore:
            try:
                result = await async_check_stream_url(channel)
                return result
            except Exception:
                return None
    
    # 增大批处理大小
    batch_size = 200
    for i in range(0, len(channels), batch_size):
        batch = list(channels)[i:i+batch_size]
        tasks = [bounded_check(channel) for channel in batch]
        
        with tqdm(total=len(batch), desc=f"检查频道批次 {i//batch_size + 1}") as pbar:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if result:
                    valid_channels.add(result)
                pbar.update(1)
        
        await asyncio.sleep(0.5)  # 减少批次间延迟
    
    return valid_channels

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='合并IPTV直播源')
    parser.add_argument('--no-test', action='store_true', 
                      help='跳过直播源测试')
    parser.add_argument('--timeout', type=int, default=3,
                      help='测试超时时间(秒)')
    parser.add_argument('--max-concurrent', type=int, default=50,
                      help='最大并发数')
    return parser.parse_args()

def reclassify_other_channels(categorized_channels):
    """重新分类其他频道分类中的频道"""
    if "其他频道" not in categorized_channels:
        return
        
    other_channels = categorized_channels["其他频道"].copy()
    categorized_channels["其他频道"].clear()
    
    # 地方频道通用后缀
    common_suffixes = [
        "综合", "新闻", "都市", "影视", "生活", "公共", "少儿", "经济", 
        "科教", "文艺", "教育", "农村", "法制", "高清", "HD", "频道",
        "文旅", "娱乐", "体育", "戏曲", "电视台", "卫视"
    ]
    
    # 地方频道关键词映射
    local_channel_patterns = {
        "广东频道": {
            "cities": [
                "广东", "广州", "深圳", "珠海", "汕头", "佛山", "韶关", "湛江", "肇庆",
                "江门", "茂名", "惠州", "梅州", "汕尾", "河源", "阳江", "清远", "东莞",
                "中山", "潮州", "揭阳", "云浮", "番禺", "花都", "增城", "从化"
            ],
            "keywords": [
                "珠江", "南方", "广视", "羊城", "荔枝", "岭南", "粤语", "广府",
                "DV生活", "现代教育", "嘉佳卡通", "移动", "有线","广州","汕头","汕头经济","汕头文旅","汕头综合"
            ]
        },
        "浙江频道": {
            "cities": [
                "浙江", "杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州",
                "舟山", "台州", "丽水", "余杭", "萧山", "临安"
            ],
            "keywords": [
                "钱江", "浙视", "留学", "教科", "民生", "休闲", "导视", "数码",
                "钱江都市", "浙江卫视", "浙江经视", "浙江新闻"
            ]
        },
        # ... 其他省份类似配置 ...
    }
    
    def match_local_channel(channel_name):
        """匹配地方频道"""
        for province, patterns in local_channel_patterns.items():
            # 检查城市名称
            for city in patterns["cities"]:
                if city in channel_name:
                    # 检查是否包含通用后缀
                    if any(suffix in channel_name for suffix in common_suffixes):
                        return province
                    # 检查是否包含特定关键词
                    if any(keyword in channel_name for keyword in patterns["keywords"]):
                        return province
                    # 如果频道名就是城市名，也归类
                    if channel_name.strip() == city:
                        return province
            
            # 检查特定关键词
            for keyword in patterns["keywords"]:
                if keyword in channel_name:
                    return province
        return None
    
    # 专题频道关键词
    topic_patterns = {
        "体育频道": [
            "体育", "足球", "篮球", "ESPN", "SPORT", "NBA", "UFC",
            "搏击", "武术", "赛车", "网球", "高尔夫", "台球"
        ],
        "影视频道": [
            "影视", "电影", "剧场", "戏曲", "电视剧", "综艺", "院线",
            "CHC", "HBO", "MOVIE", "欢笑剧场", "都市剧场"
        ],
        "新闻频道": [
            "新闻", "资讯", "财经", "CNN", "BBC", "气象", "天气",
            "环球", "时事", "直播", "实况"
        ],
        "少儿频道": [
            "少儿", "动画", "卡通", "儿童", "亲子", "幼儿", "动漫",
            "青少", "小学生", "婴幼儿"
        ]
    }
    
    for channel in other_channels:
        channel_name = channel.split(',')[0]
        
        # 先尝试匹配地方频道
        province = match_local_channel(channel_name)
        if province:
            categorized_channels[province].add(channel)
            continue
            
        # 如果不是地方频道，检查是否是专题频道
        matched = False
        for topic, keywords in topic_patterns.items():
            if any(keyword in channel_name.lower() for keyword in keywords):
                # 再次确认不是地方频道
                if not any(city in channel_name for province in local_channel_patterns.values() 
                          for city in province["cities"]):
                    categorized_channels[topic].add(channel)
                    matched = True
                    break
        
        # 如果仍然没有匹配，保留在其他频道分类
        if not matched and province is None:
            categorized_channels["其他频道"].add(channel)

def fetch_url_with_retry(url, max_retries=3, timeout=10):
    """获取URL内容，带重试机制"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': '*/*',
        'Connection': 'keep-alive'
    }
    
    # 处理代理设置
    proxies = None
    if url.startswith('https://ghgo.xyz') or url.startswith('https://iptv.b2og.com'):
        # 对特定域名使用代理
        proxies = {
            'http': None,  # 不使用 HTTP 代理
            'https': None  # 不使用 HTTPS 代理
        }
    
    for i in range(max_retries):
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=timeout,
                proxies=proxies,
                verify=False  # 禁用SSL验证
            )
            
            if response.status_code == 200:
                return response.text
            elif response.status_code == 404:
                print(f"资源不存在 (404): {url}")
                return None
            else:
                print(f"HTTP错误 {response.status_code}: {url}")
                
        except requests.exceptions.SSLError:
            print(f"SSL错误，尝试不验证证书: {url}")
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    verify=False,
                    proxies=proxies
                )
                if response.status_code == 200:
                    return response.text
            except Exception as e:
                print(f"重试 {i+1}/{max_retries} 获取 {url} 失败: {str(e)}")
                
        except requests.exceptions.ReadTimeout:
            print(f"读取超时，尝试增加超时时间: {url}")
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=timeout * 2,  # 增加超时时间
                    verify=False,
                    proxies=proxies
                )
                if response.status_code == 200:
                    return response.text
            except Exception as e:
                print(f"重试 {i+1}/{max_retries} 获取 {url} 失败: {str(e)}")
                
        except requests.exceptions.ConnectionError:
            print(f"连接错误: {url}")
        except requests.exceptions.RequestException as e:
            print(f"请求错误: {url} - {str(e)}")
            
        if i < max_retries - 1:
            wait_time = 2 ** i  # 指数退避
            print(f"等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)
            
    return None

async def test_channel_url_async(session, url, timeout=3, max_retries=2):
    """异步测试频道URL是否可用"""
    for retry in range(max_retries):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': '*/*',
                'Connection': 'keep-alive'
            }
            async with session.head(url, 
                                  timeout=timeout,
                                  headers=headers,
                                  allow_redirects=True,
                                  ssl=False) as response:
                # 接受 200-299 的状态码
                if 200 <= response.status < 300:
                    return True
                # 对于直播流，某些服务器可能返回特定状态码
                if response.status in [302, 303, 307, 308]:
                    return True
        except:
            if retry == max_retries - 1:
                return False
            await asyncio.sleep(1)  # 重试前等待
    return False

async def test_channel_latency(session, url, timeout=3, max_retries=2):
    """测试频道延迟"""
    min_latency = float('inf')
    
    for retry in range(max_retries):
        try:
            start_time = time.time()
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': '*/*',
                'Connection': 'keep-alive'
            }
            async with session.head(url, 
                                  timeout=timeout,
                                  headers=headers,
                                  allow_redirects=True,
                                  ssl=False) as response:
                if 200 <= response.status < 300 or response.status in [302, 303, 307, 308]:
                    latency = time.time() - start_time
                    min_latency = min(min_latency, latency)
            
            if min_latency != float('inf'):
                return min_latency
                
        except:
            continue
            
    return min_latency

async def validate_and_sort_channels(categorized_channels, max_test=50, max_concurrent=20):
    """异步验证频道并按延迟排序"""
    valid_channels = defaultdict(list)
    
    async with aiohttp.ClientSession() as session:
        for category, channels in categorized_channels.items():
            print(f"\n测试 {category} 频道...")
            
            test_tasks = []
            tested = 0
            
            with tqdm(total=min(len(channels), max_test), desc=f"测试进度") as pbar:
                for channel in channels:
                    if tested >= max_test:
                        break
                    
                    try:
                        name, url = channel.split(',', 1)
                        url = url.strip()
                        
                        # 先测试可用性
                        is_valid = await test_channel_url_async(session, url)
                        if is_valid:
                            # 再测试延迟
                            latency = await test_channel_latency(session, url)
                            if latency != float('inf'):
                                valid_channels[category].append((latency, channel))
                                print(f"✓ {name} (延迟: {latency:.3f}s)")
                            else:
                                # 可用但延迟高的频道也保留
                                valid_channels[category].append((float('inf'), channel))
                                print(f"△ {name} (高延迟)")
                        else:
                            print(f"✗ {name}")
                        
                        pbar.update(1)
                        tested += 1
                        
                    except Exception as e:
                        print(f"! 测试出错 {name}: {str(e)}")
                        # 测试出错的频道也保留
                        valid_channels[category].append((float('inf'), channel))
                        pbar.update(1)
                        continue
            
            # 按延迟排序，但保留所有频道
            valid_channels[category].sort(key=lambda x: x[0])
    
    # 转换回只包含频道的格式，保留所有频道
    return {category: [channel for _, channel in channels] 
            for category, channels in valid_channels.items()}

def write_channels_to_file(output_file, categorized_channels):
    """将频道写入文件"""
    try:
        with open(output_file, "w", encoding='utf-8') as file:
            # 定义分类顺序
            category_order = [
                "央视频道",
                "卫视频道",
                "浙江频道",
                "上海频道",
                "北京频道",
                "广东频道",
                "江苏频道",
                "湖南频道",
                "山东频道",
                "河南频道",
                "河北频道",
                "安徽频道",
                "四川频道",
                "内蒙频道",
                "福建频道",
                "江西频道",
                "云南频道",
                "贵州频道",
                "广西频道",
                "陕西频道",
                "甘肃频道",
                "青海频道",
                "宁夏频道",
                "新疆频道",
                "西藏频道",
                "海南频道",
                "吉林频道",
                "辽宁频道",
                "黑龙江频道",
                "山西频道",
                "天津频道",
                "重庆频道",
                "地级市频道",
                "体育频道",
                "影视频道",
                "少儿频道",
                "新闻频道",
                "其他频道"
            ]
            
            # 记录已写入的分类
            written_categories = set()
            
            for category in category_order:
                if category in categorized_channels and categorized_channels[category]:
                    channels = set()
                    for channel in categorized_channels[category]:
                        standardized = standardize_channel_name(channel)
                        if standardized and is_valid_channel(standardized):
                            channels.add(standardized)
                    
                    if channels:
                        file.write(f"{category},#genre#\n")
                        written_categories.add(category)
                        
                        # 对频道进行排序
                        sorted_channels = sorted(channels, 
                                              key=lambda x: lazy_pinyin(x.split(',')[0])[0] if ',' in x else '')
                        
                        # 特殊处理央视频道
                        if category == "央视频道":
                            cctv_channels = []
                            other_channels = []
                            
                            for channel in sorted_channels:
                                if ',' not in channel:
                                    continue
                                    
                                name = channel.split(',')[0]
                                if 'CCTV-' in name or 'CCTV' in name:
                                    match = re.match(r'CCTV-?(\d+)', name)
                                    if match:
                                        num = int(match.group(1))
                                        cctv_channels.append((num, channel))
                                    elif 'CCTV-5+' in name:
                                        cctv_channels.append((5.5, channel))
                                    else:
                                        other_channels.append(channel)
                                else:
                                    other_channels.append(channel)
                            
                            cctv_channels.sort()
                            for _, channel in cctv_channels:
                                file.write(f"{channel}\n")
                            for channel in sorted(other_channels):
                                file.write(f"{channel}\n")
                        else:
                            # 其他分类直接写入排序后的频道
                            for channel in sorted_channels:
                                if ',' in channel:
                                    file.write(f"{channel}\n")
                        
                        file.write("\n")
            
            print(f"成功写入频道到文件: {output_file}")
            print(f"共写入 {sum(len(channels) for channels in categorized_channels.values())} 个频道")
            
    except Exception as e:
        print(f"写入文件时出错: {str(e)}")
        raise

def fetch_and_merge():
    """获取并合并直播源"""
    urls = [
        "https://iptv.b2og.com/txt/fmml_ipv6.txt",
        "https://iptv.002397.xyz/txt/fmml_ipv6.txt",
        "https://m3u.ibert.me/txt/o_s_cn_cctv.txt",
        "https://iptv.b2og.com/fmml_ipv6.m3u",
        "https://ghgo.xyz/raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.txt",
        "https://ghgo.xyz/raw.githubusercontent.com/yuanzl77/IPTV/master/live.txt",
        "http://wx.thego.cn/mh.txt",
        "https://ghgo.xyz/raw.githubusercontent.com/vbskycn/iptv/master/tv/hd.txt",
        "https://iptv.b2og.com/txt/ycl_iptv.txt",
        "https://iptv.b2og.com/txt/j_home.txt",
        "http://xhztv.top/xhz/live.txt",
        "http://live.kilvn.com/iptv.m3u",
        "http://xhztv.top/new.txt",
        "https://raw.githubusercontent.com/kimwang1978/collect-tv-txt/64a2725dbc6de1a567e85e8778ff8b7dcd428d7a/live_lite.txt",
        "https://raw.githubusercontent.com/kilvn/iptv/4ab2e2f82198ad931dcdcccf995793f62c459d76/iptv.m3u",
        "https://raw.githubusercontent.com/supperthomas/TV/3e226079060fe9f099e91c12b97462281c6916e4/result.txt",
        "https://raw.githubusercontent.com/qingshujun/raoping/49396cab34c46a5a48bd916e7c54818e74fce5d8/jingzhou.txt",
        "https://raw.githubusercontent.com/kelopanda/TvSources/00ba55d187faa19509c794856a1858407b5d4609/self.txt",
        
    ]
    
    print("\n开始获取和合并直播源...")
    categorized_channels = defaultdict(set)
    
    for url in urls:
        try:
            print(f"\n获取 {url}...")
            content = fetch_url_with_retry(url)
            if not content:
                continue
                
            if url.endswith('.m3u') or url.endswith('.m3u8') or '#EXTM3U' in content:
                content = parse_m3u(content)
            
            # 处理每个频道
            for line in content.splitlines():
                line = line.strip()
                if line and not line.endswith(',#genre#'):
                    category, channel = categorize_channel(line)
                    if category and channel and is_valid_channel(channel):
                        standardized = standardize_channel_name(channel)
                        if standardized:
                            categorized_channels[category].add(standardized)
            
            print(f"已处理 {sum(len(channels) for channels in categorized_channels.values())} 个频道")
            
        except Exception as e:
            print(f"处理源 {url} 时出错: {str(e)}")
            continue
    
    print("\n开始验证频道可用性并测试延迟...")
    categorized_channels = asyncio.run(validate_and_sort_channels(categorized_channels))
    
    # 写入合并后的频道到文件
    output_file = "txt/merged_file.txt"
    write_channels_to_file(output_file, categorized_channels)
    
    return categorized_channels

def main():
    """主函数"""
    try:
        print("开始执行IPTV频道合并...")
        
        # 获取并合并频道
        categorized_channels = fetch_and_merge()
        
        print(f"\n处理完成！")
        print(f"- 总频道数: {sum(len(channels) for channels in categorized_channels.values())}")
        
    except Exception as e:
        print(f"执行过程中出错: {str(e)}")
        raise

if __name__ == "__main__":
    main()
