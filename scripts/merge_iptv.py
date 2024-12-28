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

# 在文件顶部定义provinces列表
PROVINCES = [
    "浙江", "北京", "上海", "广东", "江苏", "湖南", "山东", 
    "河南", "河北", "安徽", "福建", "重庆", "四川", "贵州", 
    "云南", "陕西", "甘肃", "青海", "内蒙古", "宁夏", "新疆",
    "西藏", "黑龙江", "吉林", "辽宁"
]

# 全局配置
CONFIG = {
    'ENABLE_TEST': True ,  # 设置为 True 开启测试，False 关闭测试
    'TIMEOUT': 3,  # 测试超时时间(秒)
    'MAX_CONCURRENT': 50,  # 最大并发数
    'BATCH_SIZE': 200,  # 批处理大小
    'MAX_RETRIES': 1  # 最大重试次数
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

def standardize_channel_name(channel_line):
    """标准化频道名称"""
    parts = channel_line.split(',', 1)
    if len(parts) != 2:
        return None
    
    channel_name, url = parts
    
    # 跳过没有名称的频道
    if not channel_name.strip() or channel_name.strip().startswith('http'):
        return None
        
    # 跳过纯数字或者太短的名称
    if channel_name.strip().isdigit() or len(channel_name.strip()) < 2:
        return None
        
    # 跳过包含乱码的频道名称
    if not is_valid_chinese_text(channel_name):
        return None
    
    # 标准化CCTV频道名称
    cctv_pattern = r'CCTV-?(\d+).*'
    cctv_match = re.match(cctv_pattern, channel_name, re.IGNORECASE)
    if cctv_match:
        channel_num = cctv_match.group(1)
        # CCTV频道名称对应表
        cctv_names = {
            '1': 'CCTV-1_综合',
            '2': 'CCTV-2_财经',
            '3': 'CCTV-3_综艺',
            '4': 'CCTV-4_中文国际',
            '5': 'CCTV-5_体育',
            '6': 'CCTV-6_电影',
            '7': 'CCTV-7_国防军事',
            '8': 'CCTV-8_电视剧',
            '9': 'CCTV-9_纪录',
            '10': 'CCTV-10_科教',
            '11': 'CCTV-11_戏曲',
            '12': 'CCTV-12_社会与法',
            '13': 'CCTV-13_新闻',
            '14': 'CCTV-14_少儿',
            '15': 'CCTV-15_音乐',
            '16': 'CCTV-16_奥林匹克',
            '17': 'CCTV-17_农业农村'
        }
        channel_name = cctv_names.get(channel_num, f'CCTV-{channel_num}')
    
    # 标准化卫视频道名称
    satellite_pattern = r'(.+)卫视.*'
    satellite_match = re.match(satellite_pattern, channel_name)
    if satellite_match:
        province = satellite_match.group(1)
        channel_name = f'{province}卫视'
    
    return f"{channel_name},{url}"

def categorize_channel(line):
    """根据频道名称对频道进行分类"""
    standardized_channel = standardize_channel_name(line)
    if not standardized_channel:
        return None, None
        
    parts = standardized_channel.split(',', 1)
    if len(parts) != 2:
        return None, None
    
    channel_name = parts[0]
    
    # 跳过含乱码的分类名��
    if not is_valid_chinese_text(channel_name):
        return "其他频道", standardized_channel
    
    # 省份和城市频道分类规则
    province_channels = {
        "广东频道": {
            "cities": [
                "广东", "广州", "深圳", "珠海", "汕头", "佛山", "韶关", "湛江", "肇庆", 
                "江门", "茂名", "惠州", "梅州", "汕尾", "河源", "阳江", "清远", "东莞", 
                "中山", "潮州", "揭阳", "云浮"
            ],
            "keywords": [
                "南方", "珠江", "广东新闻", "广东公共", "广东体育", "广东经济", "广东影视",
                "文旅", "综合", "经济", "新闻", "公共", "生活", "都市", "影视", "少儿",
                "台", "电视", "频道", "高清", "HD", "测试","汕头经济","汕头文旅","汕头综合"
            ]
        },
        "浙江频道": {
            "cities": [
                "浙江", "杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州", 
                "舟山", "台州", "丽水"
            ],
            "keywords": [
                "钱江", "浙江卫视", "浙江经视", "浙江新闻", "浙江少儿", "浙江教科", "浙江影视",
                "文旅", "综合", "经济", "新闻", "公共", "生活", "都市", "影视", "少儿",
                "台", "电视", "频道", "高清", "HD", "测试"
            ]
        },
        "北京频道": {
            "cities": [
                "北京", "BTV", "北京卫视", "北京新闻", "北京影视", "北京文艺", "北京体育", "北京生活",
                "北京科教", "北京财经", "北京青年", "北京纪实", "卡酷少儿"
            ],
            "keywords": [
                "台", "电视", "频道", "高清", "HD", "测试"
            ]
        },
        "上海频道": {
            "cities": [
                "上海", "东方", "STV", "上海卫视", "上海都市", "上海新闻", "上海教育", "上海纪实",
                "上海外语", "上海财经", "上海娱乐", "上海电视剧", "五星体育", "第一财经"
            ],
            "keywords": [
                "台", "电视", "频道", "高清", "HD", "测试"
            ]
        },
        "江苏频道": {
            "cities": [
                "江苏", "南京", "苏州", "无锡", "常州", "镇江", "南通", "扬州", "盐城", "徐州", "淮安", "连云港",
                "泰州", "宿迁", "江苏卫视", "江苏城市", "江苏综艺", "江苏影视", "江苏教育", "江苏体育"
            ],
            "keywords": [
                "台", "电视", "频道", "高清", "HD", "测试"
            ]
        },
        "湖南频道": {
            "cities": [
                "湖南", "长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界", "益阳", "郴州",
                "永州", "怀化", "娄底", "湘西", "湖南卫视", "湖南经视", "湖南都市", "湖南电视剧", "金鹰"
            ],
            "keywords": [
                "台", "电视", "频道", "高清", "HD", "测试"
            ]
        },
        "四川频道": {
            "cities": [
                "四川", "成都", "自贡", "攀枝花", "泸州", "德阳", "绵阳", "广元", "遂宁", "内江", "乐山",
                "南充", "眉山", "宜宾", "广安", "达州", "雅安", "巴中", "资阳", "阿坝", "甘孜", "凉山",
                "四川卫视", "四川新闻", "四川经济", "四川文化", "四川影视", "四川科教"
            ],
            "keywords": [
                "台", "电视", "频道", "高清", "HD", "测试"
            ]
        },
        "河南频道": {
            "cities": [
                "河南", "郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作", "濮阳", "许昌",
                "漯河", "三门峡", "南阳", "商丘", "信阳", "周口", "驻马店", "济源",
                "河南卫视", "河南都市", "河南民生", "河南新闻", "河南电视剧"
            ],
            "keywords": [
                "台", "电视", "频道", "高清", "HD", "测试"
            ]
        },
        "河北频道": {
            "cities": [
                "河北", "石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口", "承德", "沧州", "廊坊",
                "衡水", "河北卫视", "河北经济", "河北都市", "河北影视", "河北少儿"
            ],
            "keywords": [
                "台", "电视", "频道", "高清", "HD", "测试"
            ]
        },
        "山东频道": {
            "cities": [
                "山东", "济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "��宁", "泰安", "威海",
                "日照", "临沂", "德州", "聊城", "滨州", "菏泽", "山东卫视", "山东新闻", "山东综艺"
            ],
            "keywords": [
                "台", "电视", "频道", "高清", "HD", "测试"
            ]
        },
        "安徽频道": {
            "cities": [
                "安徽", "合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆", "黄山", "滁州",
                "阜阳", "宿州", "六安", "亳州", "池州", "宣城", "安徽卫视", "安徽经视", "安徽影视"
            ],
            "keywords": [
                "台", "电视", "频道", "高清", "HD", "测试"
            ]
        }
    }
    
    # 先检查是否是央视频道（优先级最高）
    if any(keyword in channel_name for keyword in [
        "CCTV", "央视", "中央电视台", "CETV", "CGTN", "中国教育", "央广","中国教育"
    ]):
        return "央视频道", standardized_channel
    
    # 然后检查是否是卫视频道（注意排除省内卫视）
    if "卫视" in channel_name and not any(
        province.replace("频道", "") in channel_name 
        for province in province_channels.keys()
    ):
        return "卫视频道", standardized_channel
    
    # 检查是否属于某个省份的地方频道
    for province, config in province_channels.items():
        # 检查城市名称
        for city in config["cities"]:
            if city in channel_name:
                # 如果频道名称包含城市名，就归类到该省
                return province, standardized_channel
        
        # 检查省级关键词
        if province.replace("频道", "") in channel_name:
            return province, standardized_channel
    
    # 检查专题频道
    categories = {
        "体育频道": [
            "体育", "SPORT", "ESPN", "NBA", "足球", "网球", "UFC", "搏击",
            "武术", "赛车", "高尔夫", "劲爆体育", "快乐垂钓", "五星体育"
        ],
        "影视频道": [
            "电影", "影视", "剧场", "CHC", "CCTV-6", "HBO", "MOVIE",
            "影院", "戏曲", "大片", "欢笑剧场", "都市剧场", "幸福剧场"
        ],
        "少儿频道": [
            "少儿", "动画", "卡通", "CCTV-14", "动漫", "青少",
            "儿童", "小学生", "婴幼儿", "亲子"
        ],
        "新闻频道": [
            "新闻", "NEWS", "CCTV-13", "资讯", "财经", "CNN",
            "BBC", "气象", "天气", "环球"
        ],
        "港澳台频道": [
            "香港", "澳门", "台湾", "TVB", "凤凰", "亚洲电视",
            "无线", "有线", "澳亚", "星空"
        ]
    }
    
    for category, keywords in categories.items():
        if any(keyword in channel_name.lower() for keyword in keywords):
            # 确保不是地方频道
            is_local = False
            for province, config in province_channels.items():
                if any(city in channel_name for city in config["cities"]):
                    is_local = True
                    return province, standardized_channel  # 如果是地方频道，直接返回省份分类
            if not is_local:
                return category, standardized_channel
    
    # 如果没有匹配到任何分类，再次检查是否包含地方台关键词
    for province, config in province_channels.items():
        if any(keyword in channel_name for keyword in config["keywords"]):
            return province, standardized_channel
    
    # 最后才归入其他频道
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

async def async_check_url(url, timeout=None, max_retries=None):
    """异步检查URL是否有效"""
    timeout = timeout or CONFIG['TIMEOUT']
    max_retries = max_retries or CONFIG['MAX_RETRIES']
    
    for retry in range(max_retries + 1):
        try:
            # 基本URL格式检查
            parsed = urlparse(url)
            if not all([parsed.scheme, parsed.netloc]):
                return False
            
            # 检查URL协议    
            if parsed.scheme not in ['http', 'https', 'rtmp', 'rtsp']:
                return False
                
            # 对于m3u8文件，只检查文件头
            if url.endswith('.m3u8'):
                connector = aiohttp.TCPConnector(force_close=True, limit=0, verify_ssl=False)
                async with aiohttp.ClientSession(connector=connector) as session:
                    try:
                        headers = {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            'Range': 'bytes=0-1024'  # 只请求前1KB
                        }
                        async with session.get(url, timeout=timeout, headers=headers) as response:
                            if response.status == 1200:
                                content = await response.content.read(1024)
                                return b'#EXTM3U' in content
                    except:
                        pass
                return False
                
            # 对于其他流媒体链接，使用HEAD请求
            connector = aiohttp.TCPConnector(force_close=True, limit=0, verify_ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                try:
                    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                    async with session.head(url, timeout=timeout, headers=headers) as response:
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
            await asyncio.sleep(0.5)  # 减少重试等待时间
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

def fetch_and_merge():
    """获取并合并播源"""
    # 更新URL列表，只保留可靠的源
    urls = [
        # GitHub直链
        "https://iptv.b2og.com/txt/fmml_ipv6.txt",
        "https://iptv.b2og.com/fmml_ipv6.m3u",
        "https://ghgo.xyz/raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.txt",
        "https://ghgo.xyz/raw.githubusercontent.com/yuanzl77/IPTV/master/live.txt",
        "https://ghgo.xyz/raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.txt",
        "https://ghgo.xyz/raw.githubusercontent.com/vbskycn/iptv/master/tv/hd.txt",
        "https://iptv.b2og.com/txt/ycl_iptv.txt",
        "https://iptv.b2og.com/txt/j_home.txt",
        "http://xhztv.top/xhz/live.txt",
        "http://live.kilvn.com/iptv.m3u",

    ]
    
    # 添加代理支持
    proxies = {
        # 如果需要代理，取消注释下面的行并填入代理地址
        # 'http': 'http://127.0.0.1:7890',
        # 'https': 'http://127.0.0.1:7890'
    }
    
    # 改进的重试机制
    def fetch_url_with_retry(url, max_retries=3, base_timeout=10):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': '*/*',
            'Connection': 'keep-alive'
        }
        
        for i in range(max_retries):
            try:
                # 随着重试次数增加超时时间
                timeout = base_timeout * (i + 1)
                response = requests.get(
                    url,
                    timeout=timeout,
                    headers=headers,
                    proxies=proxies,
                    verify=True  # 验证SSL证书
                )
                
                if response.status_code == 200:
                    return response.text
                elif response.status_code == 403:
                    print(f"访问被拒绝 (403): {url}")
                    break  # 不再重试
                elif response.status_code == 404:
                    print(f"资源不存在 (404): {url}")
                    break  # 不再重试
                    
            except requests.exceptions.SSLError:
                print(f"SSL错误: {url}")
                # 尝试不验证SSL证书
                try:
                    response = requests.get(
                        url,
                        timeout=timeout,
                        headers=headers,
                        proxies=proxies,
                        verify=False
                    )
                    if response.status_code == 200:
                        return response.text
                except Exception as e:
                    print(f"重试 {i+1}/{max_retries} 获取 {url} 失败: {str(e)}")
                    
            except requests.exceptions.RequestException as e:
                print(f"重试 {i+1}/{max_retries} 获取 {url} 失败: {str(e)}")
                if i < max_retries - 1:
                    # 使用指数退避
                    wait_time = 2 ** i
                    print(f"等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                continue
                
        return None

    # 使用defaultdict来存储不同分类的频道
    categorized_channels = defaultdict(set)
    
    # 添加URL缓存
    url_cache = {}
    
    for url in urls:
        try:
            if url in url_cache:
                content = url_cache[url]
            else:
                print(f"\n获取 {url}...")
                response = requests.get(url, timeout=5, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }, verify=False)
                
                if response.status_code == 200:
                    content = response.text
                    if url.endswith('.m3u') or url.endswith('.m3u8') or '#EXTM3U' in content:
                        content = parse_m3u(content)
                    url_cache[url] = content
                else:
                    continue
                    
            lines = content.splitlines()
            current_category = None
            current_batch = set()
            
            for line in lines:
                stripped_line = line.strip()
                if not stripped_line:
                    continue
                
                # 处理分类标题行
                if '#genre#' in stripped_line:
                    # 根据配置决定是否测试
                    if CONFIG['ENABLE_TEST'] and current_batch:
                        print(f"\nProcessing {len(current_batch)} channels in {current_category}...")
                        valid_channels = asyncio.run(process_channel_batch_async(
                            current_batch, 
                            max_concurrent=CONFIG['MAX_CONCURRENT']
                        ))
                        if valid_channels:
                            categorized_channels[current_category].update(valid_channels)
                        current_batch.clear()
                    else:
                        # 不测试时直接添加所有频道
                        if current_batch:
                            categorized_channels[current_category].update(current_batch)
                            current_batch.clear()
                    
                    current_category = standardize_category_name(stripped_line.split(',')[0])
                    continue
                
                # 处理频道行
                if stripped_line and not stripped_line.startswith('#'):
                    if current_category:
                        standardized_channel = standardize_channel_name(stripped_line)
                        if standardized_channel:
                            current_batch.add(standardized_channel)
                    else:
                        category, channel = categorize_channel(stripped_line)
                        if category and channel:
                            category = standardize_category_name(category)
                            current_batch.add(channel)
            
            # 处理最后一个分类的频道
            if current_batch:
                if CONFIG['ENABLE_TEST']:
                    print(f"\nProcessing {len(current_batch)} channels in {current_category or 'uncategorized'}...")
                    valid_channels = asyncio.run(process_channel_batch_async(
                        current_batch,
                        max_concurrent=CONFIG['MAX_CONCURRENT']
                    ))
                    if valid_channels:
                        categorized_channels[current_category or "其他频道"].update(valid_channels)
                else:
                    # 不测试时直接添加所有频道
                    categorized_channels[current_category or "其他频道"].update(current_batch)
            
        except Exception as e:
            print(f"Error fetching {url}: {e}")
    
    # 在写入文件之前进行重新分类
    reclassify_other_channels(categorized_channels)
    
    # 输出统计信息
    print("\n" + "="*50)
    total_channels = sum(len(channels) for channels in categorized_channels.values())
    print(f"\nTotal valid channels found: {total_channels}")
    for category, channels in categorized_channels.items():
        print(f"{category}: {len(channels)} channels")
    print("="*50 + "\n")
    
    # 创建输出目录
    output_dir = os.path.join(os.path.dirname(__file__), "..", "txt")
    os.makedirs(output_dir, exist_ok=True)
    
    # 更新分类顺序
    category_order = [
        "央视频道",
        "卫视频道",
        "广东频道"
    ]
    # 添加所有省份频道
    category_order.extend([f"{province}频道" for province in PROVINCES])
    # 添加其他分类
    category_order.extend([
        "港澳台频道",
        "体育频道",
        "影视频道",
        "少儿频道",
        "新闻频道",
        "其他频道"
    ])
    
    # 写入分类后的文件
    output_file = os.path.join(output_dir, "merged_file.txt")
    with open(output_file, "w", encoding='utf-8') as file:
        for category in category_order:
            if category in categorized_channels:
                # 写入分类标题
                file.write(f"{category},#genre#\n")
                # 写入该分类下的所有频道
                for channel in sorted(categorized_channels[category]):
                    file.write(channel + "\n")
                # 添加空行分隔不同分类
                file.write("\n")

if __name__ == "__main__":
    start_time = time.time()
    
    # 运行主程序
    fetch_and_merge()
    
    end_time = time.time()
    print(f"\nTotal processing time: {end_time - start_time:.2f} seconds")
