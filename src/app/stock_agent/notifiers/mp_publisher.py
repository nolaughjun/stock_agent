"""
微信公众号"发布"功能推送模块

适用于个人订阅号：
- 可以通过API发布文章
- 文章会出现在公众号历史消息中
- 用户需要主动进入公众号查看（不会收到推送通知）
- 每天可发布多次

官方文档：
https://developers.weixin.qq.com/doc/offiaccount/Publish/Publish.html

使用方法：
1. 注册微信公众号（订阅号）
2. 获取AppID和AppSecret
3. 配置IP白名单
4. 调用发布接口
"""
import os
import json
import requests
import time
import io
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, List, Dict
from pathlib import Path

# 尝试导入PIL用于生成缩略图
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# 禁用代理
NO_PROXY = {'http': None, 'https': None}


@dataclass
class MPPublishConfig:
    """公众号发布配置"""
    appid: str = ""                    # 公众号AppID
    secret: str = ""                   # 公众号AppSecret
    enabled: bool = True
    max_stocks: int = 10               # 最多推送股票数量

    @classmethod
    def from_file(cls, config_path: str = None) -> 'MPPublishConfig':
        """从配置文件加载"""
        if config_path is None:
            config_path = Path(__file__).parent.parent / "mp_publish_config.json"

        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return cls(**data)
            except Exception as e:
                print(f"加载配置文件失败: {e}")

        return cls()

    def save(self, config_path: str = None):
        """保存配置"""
        if config_path is None:
            config_path = Path(__file__).parent.parent / "mp_publish_config.json"

        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)


class MPPublisher:
    """
    微信公众号文章发布器

    功能：
    - 通过API发布文章到公众号
    - 文章会出现在公众号历史消息中
    - 用户可主动查看（不会收到推送通知）
    """

    def __init__(self, config: MPPublishConfig = None):
        self.config = config or MPPublishConfig.from_file()
        self._access_token = None
        self._token_expire_time = 0

    def _get_access_token(self) -> Optional[str]:
        """获取access_token"""
        # 检查缓存
        if self._access_token and time.time() < self._token_expire_time:
            return self._access_token

        if not self.config.appid or not self.config.secret:
            print("错误: 未配置AppID或Secret")
            return None

        try:
            url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={self.config.appid}&secret={self.config.secret}"
            response = requests.get(url, timeout=10, proxies=NO_PROXY)
            result = response.json()

            if 'errcode' in result:
                print(f"获取access_token失败: {result.get('errmsg', '未知错误')}")
                return None

            self._access_token = result['access_token']
            self._token_expire_time = time.time() + result.get('expires_in', 7200) - 300

            return self._access_token

        except Exception as e:
            print(f"获取access_token异常: {e}")
            return None

    def _generate_thumb_image(self, title: str = "ML选股") -> Optional[io.BytesIO]:
        """
        生成缩略图图片

        Args:
            title: 标题文字

        Returns:
            图片字节流
        """
        if not HAS_PIL:
            print("警告: 未安装PIL库，无法生成缩略图")
            print("请运行: pip install Pillow")
            return None

        try:
            # 创建 200x200 的图片（微信要求缩略图小于1MB）
            img = Image.new('RGB', (200, 200), color='#3498db')
            draw = ImageDraw.Draw(img)

            # 绘制渐变背景
            for i in range(200):
                color = f'#{hex(52 + i//4)[2:]}{hex(152 - i//4)[2:]}{hex(219 - i//4)[2:]}'
                draw.line([(0, i), (200, i)], fill='#3498db')

            # 添加文字
            try:
                # 尝试使用系统字体
                font = ImageFont.truetype("msyh.ttc", 24)  # 微软雅黑
            except:
                font = ImageFont.load_default()

            # 绘制标题
            draw.text((100, 80), "ML选股", fill='white', font=font, anchor='mm')
            draw.text((100, 120), "📊", fill='white', font=font, anchor='mm')

            # 保存到字节流
            img_bytes = io.BytesIO()
            img.save(img_bytes, format='PNG')
            img_bytes.seek(0)

            return img_bytes

        except Exception as e:
            print(f"生成缩略图失败: {e}")
            return None

    def _upload_thumb(self, access_token: str, image_path: str = None, title: str = "ML选股") -> Optional[str]:
        """
        上传缩略图素材

        Args:
            access_token: 访问令牌
            image_path: 图片路径（可选，不提供则使用默认Logo）
            title: 自动生成时的标题

        Returns:
            media_id
        """
        try:
            # 优先使用默认Logo
            default_logo_path = Path(__file__).parent.parent / "assets" / "logo.png"

            if image_path and os.path.exists(image_path):
                with open(image_path, 'rb') as f:
                    img_data = f.read()
                filename = os.path.basename(image_path)
            elif default_logo_path.exists():
                with open(default_logo_path, 'rb') as f:
                    img_data = f.read()
                filename = "logo.png"
            else:
                # 自动生成缩略图
                img_bytes = self._generate_thumb_image(title)
                if img_bytes is None:
                    return None
                img_data = img_bytes.read()
                filename = "thumb.png"

            # 上传到微信
            url = f"https://api.weixin.qq.com/cgi-bin/material/add_material?access_token={access_token}&type=thumb"

            files = {
                'media': (filename, img_data, 'image/png')
            }

            response = requests.post(url, files=files, timeout=30, proxies=NO_PROXY)
            result = response.json()

            if 'media_id' in result:
                print(f"缩略图上传成功: {result['media_id'][:20]}...")
                return result['media_id']
            else:
                print(f"上传缩略图失败: {result.get('errmsg', '未知错误')}")
                return None

        except Exception as e:
            print(f"上传缩略图异常: {e}")
            return None

    def _get_stock_name(self, code: str) -> str:
        """
        获取股票名称（使用腾讯财经API）

        Args:
            code: 股票代码

        Returns:
            股票名称，如果获取失败则返回代码
        """
        try:
            import requests

            # 使用腾讯财经API
            if code.startswith('6'):
                market = 'sh'
            else:
                market = 'sz'

            url = f'http://qt.gtimg.cn/q={market}{code}'
            headers = {'Referer': 'http://finance.qq.com'}
            response = requests.get(url, headers=headers, timeout=5)
            response.encoding = 'gbk'
            data = response.text

            # 解析数据：v_sh603032="1~德新科技~603032..."
            import re
            match = re.search(r'=\"(.*)\"', data)
            if match:
                items = match.group(1).split('~')
                if len(items) >= 2:
                    return items[1]  # 股票名称在第2个字段
        except:
            pass

        return code  # 如果获取失败，返回代码

    def _build_article_content(
        self,
        stocks: List[Dict],
        scan_date: str,
        market: str,
        sentiment_score: float = 50.0,
        sentiment_phase: str = "发酵"
    ) -> str:
        """
        构建文章HTML内容
        包含：一、市场情绪分析 二、选股结果 三、操作建议
        """
        stocks = stocks[:self.config.max_stocks]

        # === 一、市场情绪分析 ===
        # 情绪阶段颜色
        phase_colors = {
            '冰点': '#60a5fa',   # 蓝色
            '启动': '#34d399',   # 绿色
            '发酵': '#fbbf24',   # 黄色
            '高潮': '#f97316',   # 橙色
            '退潮': '#ef4444',   # 红色
        }
        phase_color = phase_colors.get(sentiment_phase, '#9ca3af')

        # 情绪阶段说明
        phase_desc = {
            '冰点': '市场极度低迷，观望为主',
            '启动': '市场开始回暖，可适当参与',
            '发酵': '市场情绪扩散，积极参与',
            '高潮': '市场情绪高涨，注意风险',
            '退潮': '市场情绪回落，谨慎参与',
        }
        phase_description = phase_desc.get(sentiment_phase, '')

        # === 二、选股结果表格 ===
        table_rows = ""
        for i, stock in enumerate(stocks, 1):
            ml_score = stock.get('ml_score', 0)

            if ml_score >= 0.7:
                priority = '***'
            elif ml_score >= 0.5:
                priority = '**'
            else:
                priority = '*'

            # 获取股票代码和名称
            stock_code = stock.get('stock_code', '')
            stock_name = stock.get('stock_name', '') or self._get_stock_name(stock_code)

            table_rows += f"""<tr>
<td style="text-align:center;">{priority}</td>
<td style="text-align:center;"><strong>{stock_code}</strong></td>
<td style="text-align:center;">{stock_name}</td>
<td style="text-align:center;">{stock.get('signal_date', scan_date)}</td>
<td style="text-align:center;">{stock.get('close_price', 0):.2f}</td>
<td style="text-align:center;">{stock.get('stop_loss_price', 0):.2f}</td>
<td style="text-align:center;">{ml_score:.4f}</td>
<td style="text-align:center;">{stock.get('ml_level', '-')}</td>
</tr>
"""

        # 构建完整HTML
        content = f"""
<h2>一、市场情绪分析</h2>

<table style="width:100%;border-collapse:collapse;margin-bottom:15px;">
<tr style="background:#f5f5f5;">
<td style="padding:10px;border:1px solid #ddd;"><strong>情绪评分</strong></td>
<td style="padding:10px;border:1px solid #ddd;"><strong>情绪阶段</strong></td>
<td style="padding:10px;border:1px solid #ddd;"><strong>阶段说明</strong></td>
</tr>
<tr>
<td style="padding:10px;border:1px solid #ddd;text-align:center;font-size:18px;"><strong>{sentiment_score:.1f}</strong></td>
<td style="padding:10px;border:1px solid #ddd;text-align:center;"><span style="background:{phase_color};color:white;padding:4px 12px;border-radius:4px;">{sentiment_phase}</span></td>
<td style="padding:10px;border:1px solid #ddd;">{phase_description}</td>
</tr>
</table>

<hr style="border:none;border-top:1px solid #ddd;margin:20px 0;">

<h2>二、选股结果</h2>

<p><strong>共选出 {len(stocks)} 只股票</strong></p>

<table style="width:100%;border-collapse:collapse;">
<thead>
<tr style="background:#f5f5f5;">
<th style="text-align:center;padding:8px;border:1px solid #ddd;">优先级</th>
<th style="text-align:center;padding:8px;border:1px solid #ddd;">代码</th>
<th style="text-align:center;padding:8px;border:1px solid #ddd;">名称</th>
<th style="text-align:center;padding:8px;border:1px solid #ddd;">信号日期</th>
<th style="text-align:center;padding:8px;border:1px solid #ddd;">收盘价</th>
<th style="text-align:center;padding:8px;border:1px solid #ddd;">止损价</th>
<th style="text-align:center;padding:8px;border:1px solid #ddd;">ML分数</th>
<th style="text-align:center;padding:8px;border:1px solid #ddd;">等级</th>
</tr>
</thead>
<tbody>
{table_rows}</tbody>
</table>

<hr style="border:none;border-top:1px solid #ddd;margin:20px 0;">

<h2>三、操作建议</h2>

<table style="width:100%;border-collapse:collapse;">
<tr style="background:#e8f4fd;">
<td style="padding:10px;border:1px solid #ddd;"><strong>操作项</strong></td>
<td style="padding:10px;border:1px solid #ddd;"><strong>具体建议</strong></td>
</tr>
<tr>
<td style="padding:10px;border:1px solid #ddd;"><strong>买入价</strong></td>
<td style="padding:10px;border:1px solid #ddd;">次日开盘价</td>
</tr>
<tr>
<td style="padding:10px;border:1px solid #ddd;"><strong>止损价</strong></td>
<td style="padding:10px;border:1px solid #ddd;">按表格止损价执行（约-6%）</td>
</tr>
<tr>
<td style="padding:10px;border:1px solid #ddd;"><strong>止盈策略</strong></td>
<td style="padding:10px;border:1px solid #ddd;">
+5%卖出30% → +10%卖出30% → +15%清仓
</td>
</tr>
<tr>
<td style="padding:10px;border:1px solid #ddd;"><strong>最大持仓</strong></td>
<td style="padding:10px;border:1px solid #ddd;">5个交易日</td>
</tr>
</table>

<p style="color:#999;font-size:12px;text-align:right;margin-top:20px;">
推送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
</p>
"""

        return content

    def _build_leader_article_content(
        self,
        scan_date: str,
        market: str,
        sentiment_score: float,
        sentiment_phase: str,
        two_board_candidates: List[Dict] = None,
        three_board_candidates: List[Dict] = None,
        high_board_candidates: List[Dict] = None,
        limit_up_count: int = 0,
        limit_down_count: int = 0,
        up_count: int = 0,
        down_count: int = 0,
        high_board_count: int = 0,
    ) -> str:
        """
        构建龙头情绪选股详细报告HTML内容

        包含：
        一、市场情绪分析
        二、候选股清单（按优先级排序）
        三、候选股交易策略（次日操作）
        """
        two_board_candidates = two_board_candidates or []
        three_board_candidates = three_board_candidates or []
        high_board_candidates = high_board_candidates or []

        # === 一、市场情绪分析 ===
        phase_colors = {
            '冰点': '#60a5fa',
            '启动': '#34d399',
            '发酵': '#fbbf24',
            '高潮': '#f97316',
            '退潮': '#ef4444',
        }
        phase_color = phase_colors.get(sentiment_phase, '#9ca3af')

        phase_desc = {
            '冰点': '市场极度低迷，观望为主',
            '启动': '市场开始回暖，可适当参与',
            '发酵': '市场情绪扩散，积极参与',
            '高潮': '市场情绪高涨，注意风险',
            '退潮': '市场情绪回落，谨慎参与',
        }
        phase_description = phase_desc.get(sentiment_phase, '')

        # 情绪阶段操作建议
        if sentiment_score < 40:
            position_advice = "建议空仓观望"
        elif sentiment_score < 50:
            position_advice = "建议轻仓试错（30%-50%）"
        elif sentiment_score < 70:
            position_advice = "可积极参与（60%-80%）"
        elif sentiment_score < 85:
            position_advice = "控制仓位参与（50%-70%）"
        else:
            position_advice = "最佳参与时机（80%-100%）"

        # === 二、候选股清单 ===
        # 只推送符合条件的候选股：2板和3板（高位板不符合买入条件，不推送）
        all_candidates = []
        seen_codes = set()  # 用于去重

        # 2板候选（优先级最高）
        for s in two_board_candidates:
            code = s.get('code', '')
            if code in seen_codes:
                continue  # 跳过重复股票
            seen_codes.add(code)

            risk_level = s.get('risk_level', '中风险')
            if risk_level == '低风险':
                priority = 3
                priority_star = '⭐⭐⭐'
            elif risk_level == '中风险':
                priority = 2
                priority_star = '⭐⭐'
            else:
                priority = 1
                priority_star = '⭐'
            s['_priority'] = priority
            s['_priority_star'] = priority_star
            s['_board_type'] = '2板'
            all_candidates.append(s)

        # 3板候选（优先级中等）
        for s in three_board_candidates:
            code = s.get('code', '')
            if code in seen_codes:
                continue  # 跳过重复股票
            seen_codes.add(code)

            risk_level = s.get('risk_level', '中风险')
            if risk_level == '低风险':
                priority = 2
                priority_star = '⭐⭐'
            else:
                priority = 1
                priority_star = '⭐'
            s['_priority'] = priority
            s['_priority_star'] = priority_star
            s['_board_type'] = '3板'
            all_candidates.append(s)

        # 注意：高位板（4板以上）不符合买入条件，不参与推送

        # 按优先级排序（高优先级在前），同优先级按风险分数排序（低风险优先）
        all_candidates.sort(key=lambda x: (-x.get('_priority', 0), x.get('risk_score', 100)))

        # 限制最多推送6只股票，不足6只按实际数量推送
        max_stocks = 6
        total_candidates = len(all_candidates)
        all_candidates = all_candidates[:max_stocks]

        # 构建候选股表格（最多6只）
        candidate_table_rows = ""
        for s in all_candidates:
            code = s.get('code', '')
            name = s.get('name', code)
            close = s.get('close', 0)
            float_mv = s.get('float_mv', 0)
            risk_level = s.get('risk_level', '中风险')
            risk_factors = s.get('risk_factors', [])
            risk_str = risk_factors[0] if risk_factors else '-'
            board_type = s.get('_board_type', '')
            priority_star = s.get('_priority_star', '⭐')
            sell = s.get('sell_strategy', {})
            stop_loss = sell.get('stop_loss_price', close * 0.94)

            candidate_table_rows += f"""<tr>
<td style="text-align:center;">{priority_star}</td>
<td style="text-align:center;"><strong>{code}</strong></td>
<td style="text-align:center;">{name}</td>
<td style="text-align:center;">{board_type}</td>
<td style="text-align:center;">{close:.2f}</td>
<td style="text-align:center;">{float_mv:.1f}亿</td>
<td style="text-align:center;">{risk_level}</td>
<td style="text-align:center;">{stop_loss:.2f}</td>
</tr>
"""

        # === 三、候选股交易策略详情（最多显示全部候选股） ===
        strategy_details = ""
        for i, s in enumerate(all_candidates, 1):
            code = s.get('code', '')
            name = s.get('name', code)
            close = s.get('close', 0)
            board = s.get('board', 2)
            risk_level = s.get('risk_level', '中风险')
            buy = s.get('buy_strategy', {})
            sell = s.get('sell_strategy', {})

            if not buy or not sell:
                continue

            strategy_details += f"""
<h3>{i}. {code} {name}（{board}板，{risk_level}）</h3>

<p><strong>今日收盘价：{close:.2f}元</strong></p>

<p><strong>建仓策略</strong>（总仓位{buy.get('total_position', 0.6)*100:.0f}%，{buy.get('phase_desc', '')}）</p>

<table style="width:100%;border-collapse:collapse;">
<tr style="background:#f5f5f5;">
<th style="padding:6px;border:1px solid #ddd;">批次</th>
<th style="padding:6px;border:1px solid #ddd;">价位</th>
<th style="padding:6px;border:1px solid #ddd;">比例</th>
<th style="padding:6px;border:1px solid #ddd;">条件</th>
</tr>
<tr>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">第一批</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{buy.get('batch1_price', 0):.2f}</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{buy.get('batch1_ratio', 0.4)*100:.0f}%</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{buy.get('batch1_condition', '')}</td>
</tr>
<tr>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">第二批</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{buy.get('batch2_price', 0):.2f}</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{buy.get('batch2_ratio', 0.4)*100:.0f}%</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{buy.get('batch2_condition', '')}</td>
</tr>
<tr>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">第三批</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{buy.get('batch3_price', 0):.2f}</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{buy.get('batch3_ratio', 0.2)*100:.0f}%</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{buy.get('batch3_condition', '')}</td>
</tr>
<tr style="background:#e8f4fd;">
<td style="padding:6px;border:1px solid #ddd;text-align:center;"><strong>预估均价</strong></td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;"><strong>{buy.get('avg_buy_price', 0):.2f}</strong></td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">-</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">分批建仓后均价</td>
</tr>
</table>

<p><strong>卖出策略</strong></p>

<table style="width:100%;border-collapse:collapse;">
<tr style="background:#f5f5f5;">
<th style="padding:6px;border:1px solid #ddd;">策略</th>
<th style="padding:6px;border:1px solid #ddd;">价位</th>
<th style="padding:6px;border:1px solid #ddd;">说明</th>
</tr>
<tr>
<td style="padding:6px;border:1px solid #ddd;">止损价</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;"><strong style="color:#ef4444;">{sell.get('stop_loss_price', 0):.2f}</strong></td>
<td style="padding:6px;border:1px solid #ddd;">亏损{sell.get('stop_loss_pct', 6):.0f}%止损</td>
</tr>
<tr>
<td style="padding:6px;border:1px solid #ddd;">止盈1</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{sell.get('profit_5pct', 0):.2f}</td>
<td style="padding:6px;border:1px solid #ddd;">+5%卖30%</td>
</tr>
<tr>
<td style="padding:6px;border:1px solid #ddd;">止盈2</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{sell.get('profit_10pct', 0):.2f}</td>
<td style="padding:6px;border:1px solid #ddd;">+10%卖30%</td>
</tr>
<tr>
<td style="padding:6px;border:1px solid #ddd;">止盈3</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{sell.get('profit_15pct', 0):.2f}</td>
<td style="padding:6px;border:1px solid #ddd;">+15%卖40%</td>
</tr>
<tr>
<td style="padding:6px;border:1px solid #ddd;">移动止盈</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{sell.get('trailing_trigger', 0):.2f}</td>
<td style="padding:6px;border:1px solid #ddd;">盈利5%后回撤3%止盈</td>
</tr>
<tr>
<td style="padding:6px;border:1px solid #ddd;">最大持仓</td>
<td style="padding:6px;border:1px solid #ddd;text-align:center;">{sell.get('max_hold_days', 5)}天</td>
<td style="padding:6px;border:1px solid #ddd;">到期强制卖出</td>
</tr>
</table>

<hr style="border:none;border-top:1px solid #eee;margin:15px 0;">
"""

        # 构建完整HTML
        content = f"""
<h2>一、市场情绪分析</h2>

<table style="width:100%;border-collapse:collapse;margin-bottom:15px;">
<tr style="background:#f5f5f5;">
<td style="padding:10px;border:1px solid #ddd;"><strong>情绪评分</strong></td>
<td style="padding:10px;border:1px solid #ddd;"><strong>情绪阶段</strong></td>
<td style="padding:10px;border:1px solid #ddd;"><strong>涨停/跌停</strong></td>
</tr>
<tr>
<td style="padding:10px;border:1px solid #ddd;text-align:center;font-size:18px;"><strong>{sentiment_score:.1f}分</strong></td>
<td style="padding:10px;border:1px solid #ddd;text-align:center;"><span style="background:{phase_color};color:white;padding:4px 12px;border-radius:4px;">{sentiment_phase}</span></td>
<td style="padding:10px;border:1px solid #ddd;text-align:center;">{limit_up_count} / {limit_down_count}</td>
</tr>
</table>

<p><strong>操作建议：</strong>{position_advice}</p>
<p><strong>阶段说明：</strong>{phase_description}</p>

<hr style="border:none;border-top:1px solid #ddd;margin:20px 0;">

<h2>二、候选股清单（按优先级排序）</h2>

<p>共筛选出 <strong>{total_candidates}只</strong> 符合条件的候选股{f"，以下展示TOP{len(all_candidates)}只" if total_candidates > len(all_candidates) else ""}</p>

<table style="width:100%;border-collapse:collapse;">
<thead>
<tr style="background:#f5f5f5;">
<th style="padding:8px;border:1px solid #ddd;">优先级</th>
<th style="padding:8px;border:1px solid #ddd;">代码</th>
<th style="padding:8px;border:1px solid #ddd;">名称</th>
<th style="padding:8px;border:1px solid #ddd;">连板</th>
<th style="padding:8px;border:1px solid #ddd;">收盘价</th>
<th style="padding:8px;border:1px solid #ddd;">市值</th>
<th style="padding:8px;border:1px solid #ddd;">风险</th>
<th style="padding:8px;border:1px solid #ddd;">止损价</th>
</tr>
</thead>
<tbody>
{candidate_table_rows}</tbody>
</table>

<hr style="border:none;border-top:1px solid #ddd;margin:20px 0;">

<h2>三、候选股交易策略（次日操作）</h2>

<p style="color:#666;font-size:13px;">以下为TOP5候选股的详细建仓和卖出策略</p>

{strategy_details}

<h2>四、操作纪律</h2>

<table style="width:100%;border-collapse:collapse;">
<tr style="background:#fff3cd;">
<td style="padding:10px;border:1px solid #ddd;"><strong>⚠️ 止损纪律</strong></td>
<td style="padding:10px;border:1px solid #ddd;">亏损6%必须离场，不可心存侥幸</td>
</tr>
<tr style="background:#d4edda;">
<td style="padding:10px;border:1px solid #ddd;"><strong>✅ 止盈纪律</strong></td>
<td style="padding:10px;border:1px solid #ddd;">+5%卖30%→+10%卖30%→+15%清仓</td>
</tr>
<tr style="background:#cce5ff;">
<td style="padding:10px;border:1px solid #ddd;"><strong>⏰ 时间纪律</strong></td>
<td style="padding:10px;border:1px solid #ddd;">持仓最多5天，到期强制卖出</td>
</tr>
</table>

<p style="color:#999;font-size:12px;text-align:right;margin-top:20px;">
报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
</p>
"""

        return content

    def publish_leader_article(
        self,
        scan_date: str,
        market: str,
        sentiment_score: float,
        sentiment_phase: str,
        two_board_candidates: List[Dict] = None,
        three_board_candidates: List[Dict] = None,
        high_board_candidates: List[Dict] = None,
        limit_up_count: int = 0,
        limit_down_count: int = 0,
        up_count: int = 0,
        down_count: int = 0,
        high_board_count: int = 0,
        thumb_media_id: str = None,
    ) -> Dict:
        """
        发布龙头情绪选股文章（详细版）

        Args:
            scan_date: 扫描日期
            market: 市场名称
            sentiment_score: 情绪评分
            sentiment_phase: 情绪阶段
            two_board_candidates: 2板候选股列表（含完整策略信息）
            three_board_candidates: 3板候选股列表
            high_board_candidates: 高位板候选股列表
            limit_up_count: 涨停家数
            limit_down_count: 跌停家数
            up_count: 上涨家数
            down_count: 下跌家数
            high_board_count: 3连板以上数量
            thumb_media_id: 缩略图素材ID

        Returns:
            发布结果
        """
        if not self.config.enabled:
            return {'success': False, 'message': '发布功能已禁用'}

        # 只统计2板和3板候选股（高位板不参与）
        all_candidates = (two_board_candidates or []) + (three_board_candidates or [])
        if not all_candidates:
            return {'success': False, 'message': '无符合条件的候选股'}

        # 获取access_token
        access_token = self._get_access_token()
        if not access_token:
            return {'success': False, 'message': '获取access_token失败'}

        # 自动上传缩略图
        if not thumb_media_id:
            print("正在生成并上传缩略图...")
            thumb_media_id = self._upload_thumb(access_token, title=f"{scan_date}龙头选股")

        # 构建文章内容
        content = self._build_leader_article_content(
            scan_date=scan_date,
            market=market,
            sentiment_score=sentiment_score,
            sentiment_phase=sentiment_phase,
            two_board_candidates=two_board_candidates,
            three_board_candidates=three_board_candidates,
            high_board_candidates=high_board_candidates,
            limit_up_count=limit_up_count,
            limit_down_count=limit_down_count,
            up_count=up_count,
            down_count=down_count,
            high_board_count=high_board_count,
        )

        # 候选股数量
        candidate_count = len(all_candidates)

        # 构建文章数据
        push_count = min(len(all_candidates), 6)  # 最多推送6只
        article = {
            "title": f"【龙头选股】{scan_date} {market} - {push_count}只候选",
            "author": "龙头情绪策略",
            "digest": f"情绪{sentiment_phase}({sentiment_score:.0f}分)，{push_count}只候选股",
            "content": content,
            "content_source_url": "",
            "need_open_comment": 0,
            "only_fans_can_comment": 0
        }

        if thumb_media_id:
            article["thumb_media_id"] = thumb_media_id
        else:
            return {'success': False, 'message': '缩略图上传失败'}

        # 发布
        result = self._publish_via_draft(access_token, [article])
        return result

    def publish_article(
        self,
        stocks: List[Dict],
        scan_date: str = None,
        market: str = "沪A",
        thumb_media_id: str = None,
        sentiment_score: float = 50.0,
        sentiment_phase: str = "发酵"
    ) -> Dict:
        """
        发布选股文章

        Args:
            stocks: 选股结果列表
            scan_date: 扫描日期
            market: 市场名称
            thumb_media_id: 缩略图素材ID（可选，不提供则自动生成）
            sentiment_score: 情绪评分
            sentiment_phase: 情绪阶段

        Returns:
            发布结果
        """
        if not self.config.enabled:
            return {'success': False, 'message': '发布功能已禁用'}

        if not stocks:
            return {'success': False, 'message': '无选股结果'}

        scan_date = scan_date or datetime.now().strftime("%Y-%m-%d")

        # 获取access_token
        access_token = self._get_access_token()
        if not access_token:
            return {'success': False, 'message': '获取access_token失败'}

        # 自动上传缩略图（如果没有提供）
        if not thumb_media_id:
            print("正在生成并上传缩略图...")
            thumb_media_id = self._upload_thumb(access_token, title=f"{scan_date}选股")

        # 构建文章内容（包含情绪数据）
        content = self._build_article_content(
            stocks, scan_date, market,
            sentiment_score=sentiment_score,
            sentiment_phase=sentiment_phase
        )

        # 构建文章数据
        article = {
            "title": f"【ML选股】{scan_date} {market} - {len(stocks[:self.config.max_stocks])}只",
            "author": "ML选股",
            "digest": f"共选出{len(stocks)}只股票，情绪{sentiment_phase}",
            "content": content,
            "content_source_url": "",
            "need_open_comment": 0,
            "only_fans_can_comment": 0
        }

        # 如果有缩略图
        if thumb_media_id:
            article["thumb_media_id"] = thumb_media_id
        else:
            return {'success': False, 'message': '缩略图上传失败，请检查PIL库是否安装(pip install Pillow)'}

        # 使用草稿接口发布
        result = self._publish_via_draft(access_token, [article])

        return result

    def _publish_via_draft(self, access_token: str, articles: List[Dict]) -> Dict:
        """
        创建草稿（个人订阅号只能创建草稿，需手动发布）

        步骤：
        1. 创建草稿
        2. 返回草稿ID，用户在公众号后台手动发布
        """
        try:
            # 创建草稿
            draft_url = f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={access_token}"
            draft_data = {"articles": articles}

            # 手动序列化，确保中文不被转义
            headers = {'Content-Type': 'application/json; charset=utf-8'}
            draft_response = requests.post(
                draft_url,
                data=json.dumps(draft_data, ensure_ascii=False).encode('utf-8'),
                headers=headers,
                timeout=30,
                proxies=NO_PROXY
            )
            draft_result = draft_response.json()

            if draft_result.get('errcode', 0) != 0:
                error_msg = draft_result.get('errmsg', '未知错误')
                errcode = draft_result.get('errcode', 0)

                # 错误码处理
                error_tips = {
                    40001: "AppSecret错误或不在IP白名单",
                    40125: "AppSecret错误",
                    45009: "接口调用超过限制",
                    48001: "api功能未授权",
                    87009: "需要图文封面",
                }

                tip = error_tips.get(errcode, "")
                return {
                    'success': False,
                    'message': f'创建草稿失败: {error_msg}',
                    'errcode': errcode,
                    'tip': tip
                }

            media_id = draft_result.get('media_id')

            print(f"\n{'='*60}")
            print("[SUCCESS] 草稿创建成功!")
            print(f"{'='*60}")
            print(f"草稿ID: {media_id}")
            print("\n请登录公众号后台手动发布：")
            print("1. 打开 https://mp.weixin.qq.com")
            print("2. 进入【草稿箱】")
            print("3. 找到刚才创建的草稿")
            print("4. 点击【发表】")
            print(f"{'='*60}")

            return {
                'success': True,
                'message': '草稿创建成功，请在公众号后台手动发布',
                'media_id': media_id
            }

        except Exception as e:
            return {'success': False, 'message': f'创建草稿异常: {e}'}

    def get_publish_status(self, access_token: str, publish_id: str) -> Dict:
        """查询发布状态"""
        try:
            url = f"https://api.weixin.qq.com/cgi-bin/freepublish/get?access_token={access_token}"
            response = requests.post(url, json={"publish_id": publish_id}, timeout=10, proxies=NO_PROXY)
            return response.json()
        except Exception as e:
            return {'errcode': -1, 'errmsg': str(e)}

    def get_article_list(self, access_token: str, offset: int = 0, count: int = 10) -> Dict:
        """获取已发布文章列表"""
        try:
            url = f"https://api.weixin.qq.com/cgi-bin/freepublish/batchget?access_token={access_token}"
            response = requests.post(url, json={"offset": offset, "count": count, "no_content": 1}, timeout=10, proxies=NO_PROXY)
            return response.json()
        except Exception as e:
            return {'errcode': -1, 'errmsg': str(e)}


def publish_scan_result(
    stocks: List[Dict],
    scan_date: str = None,
    market: str = "沪A",
    thumb_media_id: str = None,
    config_path: str = None
) -> Dict:
    """
    发布选股结果到公众号（便捷函数）

    Args:
        stocks: 选股结果列表
        scan_date: 扫描日期
        market: 市场名称
        thumb_media_id: 缩略图素材ID
        config_path: 配置文件路径

    Returns:
        发布结果
    """
    config = MPPublishConfig.from_file(config_path)
    publisher = MPPublisher(config)
    return publisher.publish_article(stocks, scan_date, market, thumb_media_id)


# ============ 使用示例 ============

if __name__ == "__main__":
    # 示例：发布选股结果
    test_stocks = [
        {
            'stock_code': '600519',
            'signal_date': '2025-05-30',
            'close_price': 1850.00,
            'stop_loss_price': 1739.00,
            'ml_score': 0.75,
            'ml_level': 'A'
        },
        {
            'stock_code': '000858',
            'signal_date': '2025-05-30',
            'close_price': 156.80,
            'stop_loss_price': 147.39,
            'ml_score': 0.68,
            'ml_level': 'B+'
        },
    ]

    # 配置
    config = MPPublishConfig(
        appid="你的AppID",
        secret="你的AppSecret",
        enabled=True
    )

    publisher = MPPublisher(config)

    # 发布文章
    result = publisher.publish_article(
        stocks=test_stocks,
        scan_date="2025-05-30",
        market="沪A"
    )

    print("\n发布结果:")
    print(json.dumps(result, ensure_ascii=False, indent=2))
