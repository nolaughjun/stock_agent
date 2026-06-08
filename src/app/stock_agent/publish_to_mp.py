"""
选股结果推送到微信公众号 - 使用示例

使用场景：
1. 每日收盘后自动运行选股
2. 将选股结果发布到个人订阅号
3. 用户进入公众号可查看历史消息
"""
import sys
from pathlib import Path
from datetime import datetime

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from stock_agent.notifiers.mp_publisher import MPPublisher, MPPublishConfig


def publish_scan_result_to_mp(
    stocks: list,
    scan_date: str = None,
    market: str = "沪A"
):
    """
    将选股结果发布到微信公众号

    Args:
        stocks: 选股结果列表，格式如下：
            [
                {
                    'stock_code': '600519',
                    'signal_date': '2025-05-30',
                    'close_price': 1850.00,
                    'stop_loss_price': 1739.00,
                    'ml_score': 0.75,
                    'ml_level': 'A'
                },
                ...
            ]
        scan_date: 扫描日期
        market: 市场名称
    """
    scan_date = scan_date or datetime.now().strftime("%Y-%m-%d")

    # 加载配置
    config = MPPublishConfig.from_file()
    publisher = MPPublisher(config)

    # 发布文章
    print(f"\n正在发布选股结果到公众号...")
    print(f"  日期: {scan_date}")
    print(f"  市场: {market}")
    print(f"  股票数量: {len(stocks)}")

    result = publisher.publish_article(
        stocks=stocks,
        scan_date=scan_date,
        market=market
    )

    if result.get('success'):
        print(f"\n✅ 发布成功!")
        print(f"  文章ID: {result.get('msg_data_id', 'N/A')}")
        print(f"  用户可在公众号历史消息中查看")
    else:
        print(f"\n❌ 发布失败: {result.get('message', '未知错误')}")
        if result.get('tip'):
            print(f"  提示: {result.get('tip')}")

    return result


# ============ 集成示例 ============

def example_usage():
    """使用示例"""
    # 模拟选股结果
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
        {
            'stock_code': '601318',
            'signal_date': '2025-05-30',
            'close_price': 45.60,
            'stop_loss_price': 42.86,
            'ml_score': 0.62,
            'ml_level': 'B'
        },
    ]

    # 发布到公众号
    result = publish_scan_result_to_mp(
        stocks=test_stocks,
        scan_date="2025-05-30",
        market="沪A"
    )

    return result


if __name__ == "__main__":
    # 测试发布
    print("=" * 60)
    print("微信公众号选股结果发布测试")
    print("=" * 60)

    # 检查配置
    config = MPPublishConfig.from_file()
    if not config.appid or config.appid == "在此填写你的AppID":
        print("\n⚠️ 请先配置 mp_publish_config.json 文件")
        print("   1. 登录微信公众平台 mp.weixin.qq.com")
        print("   2. 进入 开发 → 基本配置")
        print("   3. 复制 AppID 和 AppSecret 到配置文件")
        print("   4. 配置服务器IP白名单")
        sys.exit(1)

    # 执行发布
    example_usage()
