"""
量化策略优化建议 - V5版本规划

当前V4策略与主流量化交易的差距分析及优化方向

## 一、核心指标差距

| 指标 | V4策略 | 主流量化 | 顶级量化 | 优化目标 |
|------|--------|---------|---------|---------|
| 年化收益率 | 2.4% | 15-25% | 30-60% | 15%+ |
| 夏普比率 | 0.5 | 1.5-2.0 | 2.0-3.0 | 1.5+ |
| 胜率 | 52-62% | 55-65% | 60-70% | 60%+ |
| 盈亏比 | 1.0 | 1.5-2.0 | 2.0-3.0 | 1.5+ |
| 最大回撤 | 未统计 | 10-20% | 5-15% | <15% |

## 二、主要差距及优化方向

### 1. 止损策略优化

当前问题：固定6%止损过于简单

优化方案：
- 动态止损：根据ATR调整止损幅度
- 时间止损：持仓时间越长，止损越紧
- 移动止损：盈利后动态上移止损线
- 分级止损：
  - 第1天：-8%（给足波动空间）
  - 第2天：-7%
  - 第3天：-6%
  - 第4天+：-5%（收紧止损）

### 2. 多因子模型增强

当前因子（4个）：
- 连板数（动量因子）
- 封板强度（技术因子）
- 市值（规模因子）
- 情绪分（情绪因子）

建议增加因子：
- 成交量变化率（流动性）
- 换手率（活跃度）
- 近期波动率（风险）
- 板块联动强度（行业动量）
- 资金流向（主力资金）
- 技术形态（突破/回踩）

### 3. 组合优化

当前问题：等权重买入，未考虑相关性

优化方案：
- 基于协方差矩阵的风险预算
- 行业中性约束（单一行业不超过30%）
- 个股权重上限（单只不超过10%）
- 动态仓位调整（根据市场环境）

### 4. 执行优化

当前问题：简单开盘/低吸买入

优化方案：
- TWAP/VWAP算法拆单
- 实时监控买卖盘口
- 避开开盘集合竞价剧烈波动
- 分批建仓降低冲击成本

### 5. 风险管理增强

当前缺失：
- 最大回撤控制
- 单日亏损限制
- 连续亏损熔断

建议添加：
- 日内止损：单日亏损超过3%停止交易
- 周止损：单周亏损超过5%降低仓位
- 月止损：单月亏损超过8%暂停策略
- 回撤预警：回撤达到10%减仓50%

## 三、V5版本优化优先级

### 第一优先级（核心优化）
1. 动态止损策略
2. 盈亏比优化（让盈利奔跑）
3. 最大回撤控制

### 第二优先级（增强优化）
4. 多因子模型
5. 组合权重优化
6. 行业分散约束

### 第三优先级（精细化优化）
7. 执行算法优化
8. 交易成本建模
9. 实时风控系统

## 四、预期效果

优化后预期指标：
- 年化收益率：10-15%
- 夏普比率：1.0-1.5
- 最大回撤：<15%
- 胜率：55-65%
- 盈亏比：1.3-1.5

## 五、参考文献

1. 《主动投资组合管理》- Grinold & Kahn
2. 《量化投资策略》- 丁鹏
3. 《打开量化投资的黑箱》- 里什·纳兰
"""

# 优化后的止损策略示例
def calculate_dynamic_stop_loss(
    entry_price: float,
    atr: float,
    holding_days: int,
    max_profit_pct: float,
    volatility: float,
) -> dict:
    """
    动态止损策略

    Args:
        entry_price: 入场价格
        atr: 平均真实波幅
        holding_days: 已持仓天数
        max_profit_pct: 最高盈利百分比
        volatility: 当前波动率

    Returns:
        止损策略配置
    """
    # 基础止损：基于ATR
    atr_stop = entry_price - 2 * atr

    # 时间止损：持仓越久，止损越紧
    time_stop_pct = max(0.03, 0.08 - holding_days * 0.01)  # 3%-8%
    time_stop = entry_price * (1 - time_stop_pct)

    # 移动止损：盈利后保护利润
    if max_profit_pct >= 0.05:
        trailing_stop = entry_price * (1 + max_profit_pct - 0.03)  # 保护盈利
    else:
        trailing_stop = None

    # 波动率调整：高波动时止损更宽
    if volatility > 0.03:  # 日波动率>3%
        atr_stop *= 0.98

    # 取最严格的止损
    stop_loss = max(atr_stop, time_stop)
    if trailing_stop:
        stop_loss = max(stop_loss, trailing_stop)

    return {
        'stop_loss_price': round(stop_loss, 2),
        'stop_loss_pct': round((entry_price - stop_loss) / entry_price * 100, 2),
        'stop_type': 'trailing' if trailing_stop else 'dynamic',
    }


# 盈亏比优化示例
def optimize_profit_taking(
    entry_price: float,
    current_price: float,
    holding_days: int,
    volatility: float,
    volume_ratio: float,  # 量比
) -> dict:
    """
    动态止盈策略 - 让盈利奔跑

    核心思路：
    1. 盈利初期：快速止盈部分仓位
    2. 盈利中期：移动止盈保护利润
    3. 盈利后期：趋势跟踪，放宽止盈

    Args:
        entry_price: 入场价格
        current_price: 当前价格
        holding_days: 持仓天数
        volatility: 波动率
        volume_ratio: 量比

    Returns:
        止盈策略配置
    """
    profit_pct = (current_price - entry_price) / entry_price * 100

    if profit_pct < 0.03:  # 盈利<3%
        return {'action': 'hold', 'reason': '盈利不足，继续持有'}

    elif profit_pct < 0.05:  # 盈利3-5%
        return {
            'action': 'sell_partial',
            'sell_ratio': 0.2,
            'reason': '初期盈利，卖出20%锁定部分利润'
        }

    elif profit_pct < 0.10:  # 盈利5-10%
        return {
            'action': 'sell_partial',
            'sell_ratio': 0.3,
            'trailing_stop': entry_price * 1.02,  # 移动止损到+2%
            'reason': '中期盈利，卖出30%，移动止损保护'
        }

    elif profit_pct < 0.15:  # 盈利10-15%
        return {
            'action': 'sell_partial',
            'sell_ratio': 0.3,
            'trailing_stop': entry_price * 1.05,  # 移动止损到+5%
            'reason': '稳定盈利，卖出30%，保护5%利润'
        }

    else:  # 盈利>15%
        return {
            'action': 'trailing',
            'trailing_stop': current_price * 0.97,  # 从最高点回撤3%止盈
            'reason': '大额盈利，趋势跟踪，回撤3%止盈'
        }


# 组合优化示例
def optimize_portfolio(
    candidates: list,
    max_position: float = 0.1,  # 单只股票最大仓位
    max_industry: float = 0.3,  # 单一行业最大仓位
    total_capital: float = 1000000,
) -> dict:
    """
    组合优化

    目标：最大化风险调整后收益

    约束：
    1. 单只股票权重 <= max_position
    2. 单一行业权重 <= max_industry
    3. 总仓位 <= 100%

    Args:
        candidates: 候选股票列表
        max_position: 单只股票最大仓位比例
        max_industry: 单一行业最大仓位比例
        total_capital: 总资金

    Returns:
        优化后的仓位配置
    """
    # 按评分排序
    sorted_candidates = sorted(candidates, key=lambda x: x['score'], reverse=True)

    positions = {}
    industry_weights = {}
    remaining_capital = total_capital

    for stock in sorted_candidates:
        if remaining_capital <= 0:
            break

        code = stock['code']
        industry = stock.get('industry', 'unknown')
        score = stock['score']

        # 计算建议仓位
        suggested_position = min(
            total_capital * max_position,  # 单只股票上限
            remaining_capital,  # 剩余资金
        )

        # 检查行业约束
        current_industry_weight = industry_weights.get(industry, 0)
        if current_industry_weight + suggested_position > total_capital * max_industry:
            suggested_position = total_capital * max_industry - current_industry_weight

        if suggested_position > 0:
            positions[code] = {
                'position': suggested_position,
                'weight': suggested_position / total_capital,
                'score': score,
                'industry': industry,
            }
            industry_weights[industry] = current_industry_weight + suggested_position
            remaining_capital -= suggested_position

    return {
        'positions': positions,
        'cash': remaining_capital,
        'industry_weights': industry_weights,
        'num_stocks': len(positions),
    }
