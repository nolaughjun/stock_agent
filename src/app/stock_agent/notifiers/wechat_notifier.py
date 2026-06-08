"""
微信推送模块

支持的推送方式:
1. 微信公众号 (mp) - 推荐
2. 企业微信机器人
3. WxPusher

使用方法:
1. 注册微信公众号（订阅号）
2. 获取AppID和AppSecret
3. 配置并发布
"""
import os
import json
import requests
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, List, Dict
from pathlib import Path

# 禁用代理的配置（避免代理连接失败导致推送失败）
NO_PROXY = {
    'http': None,
    'https': None,
}


@dataclass
class WechatConfig:
    """微信推送配置"""
    # 推送方式: mp (公众号) / wework / wxpusher
    method: str = "mp"

    # 企业微信机器人配置 - 支持多个机器人
    wework_webhook: str = ""                        # 单机器人（兼容旧配置）
    wework_webhooks: List[str] = None               # 多机器人Webhook列表

    # WxPusher配置 - 支持多用户 (https://wxpusher.zjiecode.com/)
    wxpusher_token: str = ""
    wxpusher_uids: List[str] = None                 # 多用户UID列表

    # 微信公众号配置
    mp_appid: str = ""                              # 公众号AppID
    mp_secret: str = ""                             # 公众号AppSecret
    mp_template_id: str = ""                        # 模板消息ID
    mp_openids: List[str] = None                    # 用户OpenID列表

    # 推送设置
    enabled: bool = False  # 默认不推送
    max_stocks: int = 10  # 最多推送股票数量

    def __post_init__(self):
        if self.wxpusher_uids is None:
            self.wxpusher_uids = []
        if self.wework_webhooks is None:
            self.wework_webhooks = []
        if self.mp_openids is None:
            self.mp_openids = []

    @classmethod
    def from_file(cls, config_path: str = None) -> 'WechatConfig':
        """从配置文件加载"""
        if config_path is None:
            config_path = Path(__file__).parent.parent / "wechat_config.json"

        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return cls(**data)
            except Exception as e:
                print(f"加载配置文件失败: {e}")

        return cls()

    def save(self, config_path: str = None):
        """保存配置到文件"""
        if config_path is None:
            config_path = Path(__file__).parent.parent / "wechat_config.json"

        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    def get_all_webhooks(self) -> List[str]:
        """获取所有企业微信Webhook"""
        hooks = []
        if self.wework_webhook:
            hooks.append(self.wework_webhook)
        hooks.extend(self.wework_webhooks)
        return list(set(hooks))  # 去重

    def get_all_openids(self) -> List[str]:
        """获取所有公众号用户OpenID"""
        return list(set(self.mp_openids))  # 去重


class WechatNotifier:
    """
    微信推送通知器

    支持多种推送方式，将选股结果推送到微信
    """

    def __init__(self, config: WechatConfig = None):
        """
        初始化推送器

        Args:
            config: 推送配置
        """
        self.config = config or WechatConfig.from_file()

    def send_scan_result(
        self,
        stocks: List[Dict],
        scan_date: str = None,
        market: str = "创业板"
    ) -> bool:
        """
        推送选股结果

        Args:
            stocks: 选股结果列表
            scan_date: 扫描日期
            market: 市场名称

        Returns:
            是否推送成功
        """
        if not self.config.enabled:
            print("推送功能已禁用")
            return False

        if not stocks:
            print("无选股结果，跳过推送")
            return False

        scan_date = scan_date or datetime.now().strftime("%Y-%m-%d")
        title = f"【ML选股】{scan_date} {market}"

        # 构建内容
        content = self._build_scan_content(stocks, scan_date, market)

        # 根据推送方式发送
        if self.config.method == "wework":
            return self._send_wework(title, content)
        elif self.config.method == "wxpusher":
            return self._send_wxpusher(title, content)
        elif self.config.method == "mp":
            return self._send_mp(title, stocks, scan_date, market)
        elif self.config.method == "mp-mass":
            return self._send_mp_mass(title, stocks, scan_date, market)
        else:
            print(f"未知的推送方式: {self.config.method}")
            return False

    def _build_scan_content(
        self,
        stocks: List[Dict],
        scan_date: str,
        market: str
    ) -> str:
        """构建选股结果内容"""
        # 限制推送数量
        stocks = stocks[:self.config.max_stocks]

        lines = []
        lines.append(f"### {scan_date} {market}选股结果")
        lines.append("")
        lines.append(f"**共选出 {len(stocks)} 只股票**")
        lines.append("")
        lines.append("| 优先级 | 代码 | 信号日期 | 收盘价 | 止损价 | ML分数 | 等级 |")
        lines.append("|:------:|:----:|:--------:|:------:|:------:|:------:|:----:|")

        for i, stock in enumerate(stocks, 1):
            # 根据ML分数计算优先级
            ml_score = stock.get('ml_score', 0)
            if ml_score >= 0.7:
                priority = '⭐⭐⭐'
            elif ml_score >= 0.5:
                priority = '⭐⭐'
            else:
                priority = '⭐'
            lines.append(
                f"| {priority} | {stock.get('stock_code', '')} | "
                f"{stock.get('signal_date', '')} | "
                f"{stock.get('close_price', 0):.2f} | "
                f"{stock.get('stop_loss_price', 0):.2f} | "
                f"{stock.get('ml_score', 0):.4f} | "
                f"{stock.get('ml_level', '')} |"
            )

        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("**操作建议:**")
        lines.append("1. 买入价: 次日开盘价")
        lines.append("2. 止损价: 按表格执行")
        lines.append("3. 止盈: +5%(卖30%) → +10%(卖30%) → +15%(卖40%)")
        lines.append("")
        lines.append(f"*推送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        return "\n".join(lines)

    def _send_wework(self, title: str, content: str) -> bool:
        """
        企业微信机器人推送 - 支持多个机器人

        文档: https://developer.work.weixin.qq.com/document/path/91770
        """
        webhooks = self.config.get_all_webhooks()

        if not webhooks:
            print("错误: 未配置企业微信机器人Webhook")
            return False

        success_count = 0
        total_count = len(webhooks)

        for i, webhook in enumerate(webhooks, 1):
            try:
                # 构建Markdown消息
                markdown_content = f"## {title}\n\n{content}"

                response = requests.post(
                    webhook,
                    json={
                        "msgtype": "markdown",
                        "markdown": {
                            "content": markdown_content
                        }
                    },
                    timeout=10,
                    proxies=NO_PROXY  # 禁用代理
                )

                result = response.json()

                if result.get('errcode') == 0:
                    success_count += 1
                    print(f"  机器人{i}推送成功")
                else:
                    print(f"  机器人{i}推送失败: {result.get('errmsg', '未知错误')}")

            except Exception as e:
                print(f"  机器人{i}推送异常: {e}")

        print(f"企业微信推送完成: {success_count}/{total_count} 成功")
        return success_count > 0

    def _send_wxpusher(self, title: str, content: str) -> bool:
        """
        WxPusher推送

        文档: https://wxpusher.zjiecode.com/docs/
        """
        if not self.config.wxpusher_token:
            print("错误: 未配置WxPusher Token")
            return False

        if not self.config.wxpusher_uids:
            print("错误: 未配置WxPusher UIDs")
            return False

        try:
            response = requests.post(
                "http://wxpusher.zjiecode.com/api/send/message",
                json={
                    "appToken": self.config.wxpusher_token,
                    "content": f"<h3>{title}</h3><pre>{content}</pre>",
                    "contentType": 2,  # HTML
                    "uids": self.config.wxpusher_uids,
                },
                timeout=10,
                proxies=NO_PROXY  # 禁用代理
            )

            result = response.json()

            if result.get('code') == 1000:
                print(f"WxPusher推送成功: {title}")
                return True
            else:
                print(f"WxPusher推送失败: {result.get('msg', '未知错误')}")
                return False

        except Exception as e:
            print(f"WxPusher推送异常: {e}")
            return False

    def _send_mp(
        self,
        title: str,
        stocks: List[Dict],
        scan_date: str,
        market: str
    ) -> bool:
        """
        微信公众号模板消息推送

        文档: https://developers.weixin.qq.com/doc/offiaccount/Message_Management/Template_Message_Interface.html
        """
        if not self.config.mp_appid or not self.config.mp_secret:
            print("错误: 未配置公众号AppID或Secret")
            return False

        if not self.config.mp_template_id:
            print("错误: 未配置模板消息ID")
            return False

        openids = self.config.get_all_openids()
        if not openids:
            print("错误: 未配置用户OpenID")
            return False

        # 获取access_token
        try:
            token_url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={self.config.mp_appid}&secret={self.config.mp_secret}"
            token_response = requests.get(token_url, timeout=10, proxies=NO_PROXY)
            token_result = token_response.json()

            if 'errcode' in token_result:
                print(f"获取access_token失败: {token_result.get('errmsg', '未知错误')}")
                return False

            access_token = token_result['access_token']
        except Exception as e:
            print(f"获取access_token异常: {e}")
            return False

        # 构建模板消息数据
        stocks = stocks[:self.config.max_stocks]
        stock_list = "\n".join([
            f"{i}. {s.get('stock_code', '')} | {s.get('close_price', 0):.2f}元 | ML:{s.get('ml_score', 0):.2f}"
            for i, s in enumerate(stocks, 1)
        ])

        # 发送模板消息
        success_count = 0
        total_count = len(openids)

        for openid in openids:
            try:
                data = {
                    "touser": openid,
                    "template_id": self.config.mp_template_id,
                    "data": {
                        "first": {
                            "value": f"{scan_date} {market}选股结果",
                            "color": "#173177"
                        },
                        "keyword1": {
                            "value": scan_date,
                            "color": "#173177"
                        },
                        "keyword2": {
                            "value": market,
                            "color": "#173177"
                        },
                        "keyword3": {
                            "value": f"{len(stocks)}只",
                            "color": "#173177"
                        },
                        "keyword4": {
                            "value": stock_list[:200] if len(stock_list) > 200 else stock_list,
                            "color": "#173177"
                        },
                        "remark": {
                            "value": "点击查看详情，次日开盘买入，严格执行止损。",
                            "color": "#FF0000"
                        }
                    }
                }

                send_url = f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={access_token}"
                response = requests.post(send_url, json=data, timeout=10, proxies=NO_PROXY)
                result = response.json()

                if result.get('errcode') == 0:
                    success_count += 1
                    print(f"  用户 {openid[:8]}**** 推送成功")
                else:
                    print(f"  用户 {openid[:8]}**** 推送失败: {result.get('errmsg', '未知错误')}")

            except Exception as e:
                print(f"  用户 {openid[:8]}**** 推送异常: {e}")

        print(f"公众号推送完成: {success_count}/{total_count} 成功")
        return success_count > 0

    def _send_mp_mass(
        self,
        title: str,
        stocks: List[Dict],
        scan_date: str,
        market: str
    ) -> bool:
        """
        微信公众号群发消息（推送给所有关注者）

        文档: https://developers.weixin.qq.com/doc/offiaccount/Message_Management/Batch_Sends_and_Originality_Checks.html

        注意: 需要认证的服务号才能使用群发功能
        """
        if not self.config.mp_appid or not self.config.mp_secret:
            print("错误: 未配置公众号AppID或Secret")
            return False

        # 获取access_token
        try:
            token_url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={self.config.mp_appid}&secret={self.config.mp_secret}"
            token_response = requests.get(token_url, timeout=10, proxies=NO_PROXY)
            token_result = token_response.json()

            if 'errcode' in token_result:
                print(f"获取access_token失败: {token_result.get('errmsg', '未知错误')}")
                return False

            access_token = token_result['access_token']
        except Exception as e:
            print(f"获取access_token异常: {e}")
            return False

        # 构建图文消息内容
        stocks = stocks[:self.config.max_stocks]
        stock_list = "\n".join([
            f"| {i} | {s.get('stock_code', '')} | {s.get('close_price', 0):.2f} | {s.get('ml_score', 0):.2f} | {s.get('ml_level', '')} |"
            for i, s in enumerate(stocks, 1)
        ])

        content_html = f"""
<h3>{scan_date} {market}选股结果</h3>
<p>共选出 <strong>{len(stocks)}只</strong> 股票</p>
<table style="width:100%;border-collapse:collapse;">
<tr style="background:#f5f5f5;"><th>序号</th><th>代码</th><th>收盘价</th><th>ML分数</th><th>等级</th></tr>
{"".join([f"<tr><td>{i}</td><td>{s.get('stock_code', '')}</td><td>{s.get('close_price', 0):.2f}</td><td>{s.get('ml_score', 0):.2f}</td><td>{s.get('ml_level', '')}</td></tr>" for i, s in enumerate(stocks, 1)])}
</table>
<h4>操作建议:</h4>
<ul>
<li>买入价: 次日开盘价</li>
<li>止损价: 见上表止损价</li>
<li>止盈: +5%(卖30%) → +10%(卖30%) → +15%(卖40%)</li>
</ul>
<p><small>推送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small></p>
"""

        # 上传图文消息素材
        try:
            articles = [
                {
                    "title": f"【ML选股】{scan_date} {market}",
                    "thumb_media_id": self._get_default_thumb(access_token),  # 缩略图
                    "author": "ML选股系统",
                    "digest": f"共选出{len(stocks)}只股票，点击查看详情",
                    "content": content_html,
                    "content_source_url": "",
                    "need_open_comment": 0,
                    "only_fans_can_comment": 0
                }
            ]

            upload_url = f"https://api.weixin.qq.com/cgi-bin/media/uploadnews?access_token={access_token}"
            upload_response = requests.post(upload_url, json={"articles": articles}, timeout=10, proxies=NO_PROXY)
            upload_result = upload_response.json()

            if upload_result.get('errcode', 0) != 0:
                print(f"上传素材失败: {upload_result.get('errmsg', '未知错误')}")
                # 尝试使用文本消息群发
                return self._send_mp_mass_text(access_token, stocks, scan_date, market)

            media_id = upload_result['media_id']

        except Exception as e:
            print(f"上传素材异常: {e}")
            # 尝试使用文本消息群发
            return self._send_mp_mass_text(access_token, stocks, scan_date, market)

        # 群发消息
        try:
            mass_url = f"https://api.weixin.qq.com/cgi-bin/message/mass/sendall?access_token={access_token}"
            mass_data = {
                "filter": {
                    "is_to_all": True,  # 发送给所有用户
                    "tag_id": ""  # 空表示所有用户
                },
                "mpnews": {
                    "media_id": media_id
                },
                "msgtype": "mpnews",
                "send_ignore_reprint": 0
            }

            response = requests.post(mass_url, json=mass_data, timeout=10, proxies=NO_PROXY)
            result = response.json()

            if result.get('errcode') == 0:
                msg_id = result.get('msg_id', '')
                print(f"公众号群发成功! msg_id: {msg_id}")
                print(f"  预计送达: 所有关注用户")
                return True
            else:
                print(f"公众号群发失败: {result.get('errmsg', '未知错误')}")
                # 错误码说明
                errcode = result.get('errcode', 0)
                if errcode == 45028:
                    print("  提示: 群发功能需要认证的服务号")
                elif errcode == 48004:
                    print("  提示: 接口权限不足，请检查公众号类型")
                return False

        except Exception as e:
            print(f"公众号群发异常: {e}")
            return False

    def _send_mp_mass_text(
        self,
        access_token: str,
        stocks: List[Dict],
        scan_date: str,
        market: str
    ) -> bool:
        """
        公众号文本消息群发（备用方案）
        """
        stocks = stocks[:self.config.max_stocks]
        stock_text = "\n".join([
            f"{i}. {s.get('stock_code', '')} | {s.get('close_price', 0):.2f}元 | ML:{s.get('ml_score', 0):.2f}"
            for i, s in enumerate(stocks, 1)
        ])

        content = f"""{scan_date} {market}选股结果

共选出 {len(stocks)} 只股票:
{stock_text}

操作建议:
- 买入价: 次日开盘价
- 止损价: 严格执行
- 止盈: +5%→+10%→+15%"""

        try:
            mass_url = f"https://api.weixin.qq.com/cgi-bin/message/mass/sendall?access_token={access_token}"
            mass_data = {
                "filter": {
                    "is_to_all": True
                },
                "text": {
                    "content": content
                },
                "msgtype": "text"
            }

            response = requests.post(mass_url, json=mass_data, timeout=10, proxies=NO_PROXY)
            result = response.json()

            if result.get('errcode') == 0:
                print(f"公众号文本群发成功!")
                return True
            else:
                print(f"公众号群发失败: {result.get('errmsg', '未知错误')}")
                errcode = result.get('errcode', 0)
                if errcode == 45028:
                    print("  提示: 群发功能需要认证的服务号")
                elif errcode == 48004:
                    print("  提示: 接口权限不足")
                return False

        except Exception as e:
            print(f"文本群发异常: {e}")
            return False

    def _get_default_thumb(self, access_token: str) -> str:
        """
        获取默认缩略图media_id
        如果没有永久素材，返回空（会使用默认图）
        """
        # 这里应该上传一个永久素材作为缩略图
        # 暂时返回空，需要用户自行在公众号后台上传素材
        return ""

    def send_custom_message(self, title: str, content: str) -> bool:
        """
        发送自定义消息

        Args:
            title: 消息标题
            content: 消息内容

        Returns:
            是否发送成功
        """
        if not self.config.enabled:
            return False

        if self.config.method == "wework":
            return self._send_wework(title, content)
        elif self.config.method == "wxpusher":
            return self._send_wxpusher(title, content)
        elif self.config.method == "mp":
            # 公众号需要特殊处理
            print("公众号推送请使用 send_scan_result 方法")
            return False

        return False

    def test_connection(self) -> bool:
        """测试推送连接"""
        title = "ML选股系统 - 测试消息"
        content = f"这是一条测试消息\n\n发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        result = self.send_custom_message(title, content)

        if result:
            print("推送测试成功!")
        else:
            print("推送测试失败!")

        return result


def create_wechat_config(
    method: str = "mp",
    wework_webhook: str = "",
    wxpusher_token: str = "",
    wxpusher_uids: List[str] = None,
    mp_appid: str = "",
    mp_secret: str = "",
    config_path: str = None,
):
    """
    创建并保存微信推送配置

    Args:
        method: 推送方式 (mp/wework/wxpusher)
        wework_webhook: 企业微信Webhook
        wxpusher_token: WxPusher Token
        wxpusher_uids: WxPusher用户ID列表
        mp_appid: 公众号AppID
        mp_secret: 公众号AppSecret
        config_path: 配置文件路径
    """
    config = WechatConfig(
        method=method,
        wework_webhook=wework_webhook,
        wxpusher_token=wxpusher_token,
        wxpusher_uids=wxpusher_uids or [],
        mp_appid=mp_appid,
        mp_secret=mp_secret,
    )

    config.save(config_path)
    print(f"配置已保存")

    return config


def push_scan_result(
    stocks: List[Dict],
    scan_date: str = None,
    market: str = "创业板",
    config_path: str = None,
) -> bool:
    """
    推送选股结果到微信（便捷函数）

    Args:
        stocks: 选股结果列表
        scan_date: 扫描日期
        market: 市场名称
        config_path: 配置文件路径

    Returns:
        是否推送成功
    """
    config = WechatConfig.from_file(config_path)
    notifier = WechatNotifier(config)
    return notifier.send_scan_result(stocks, scan_date, market)
