# ML策略选股系统

基于机器学习的A股选股策略系统，支持创业板、沪A、深A市场。

## 功能

1. **ML策略回测** - 回测双策略信号配对 + ML信号质量预测
2. **每日选股扫描** - 扫描当日信号，选出次日可操作的股票
3. **模型训练** - 训练/重训练ML模型
4. **模型评估** - 评估模型性能
5. **定时重训练** - 自动检测并重训练模型
6. **微信推送** - 将选股结果推送到微信

## 支持市场

| 市场类型 | 参数值 | 代码前缀 | 说明 |
|----------|--------|----------|------|
| 创业板 | `gem` | 300xxx | 高成长性企业 |
| 沪A | `sha` | 600xxx, 601xxx, 603xxx, 605xxx | 上海主板 |
| 深A | `sza` | 000xxx, 001xxx, 002xxx, 003xxx | 深圳主板+中小板 |
| 全部A股 | `all` | 以上所有 | 综合市场 |

## 目录结构

```
stock_agent/
├── __init__.py
├── main.py                      # 主程序入口
├── wechat_config.json           # 微信推送配置
├── data/
│   ├── __init__.py
│   └── data_loader.py           # 数据加载器（通达信本地数据）
├── strategies/
│   ├── __init__.py
│   ├── base_strategy.py         # 基础策略类
│   ├── advanced_ml_strategy.py  # ML策略核心代码
│   ├── model_trainer.py         # 模型训练器
│   ├── signal_model.joblib      # 训练好的ML模型
│   ├── ML_STRATEGY_MANUAL.md    # 使用说明
│   └── ML_STRATEGY_CHANGELOG.md # 变更记录
├── notifiers/
│   ├── __init__.py
│   └── wechat_notifier.py       # 微信推送模块
└── utils/
    ├── __init__.py
    └── logger.py                # 日志工具
```

## 安装依赖

```bash
pip install pandas numpy scikit-learn joblib
```

## 数据源配置

本系统使用**通达信本地数据**。请确保：

1. 已安装通达信软件
2. 通达信安装在以下路径之一：
   - `C:\new_tdx`
   - `D:\new_tdx`
   - `C:\tdx`
   - `D:\tdx`

或者通过代码指定路径：

```python
from stock_agent.data.data_loader import DataLoader
loader = DataLoader(tdx_path='E:/通达信')
```

## 使用方法

### 1. ML策略回测

```bash
# 基本用法（默认创业板）
python src/app/stock_agent/main.py ml

# 指定市场回测
python src/app/stock_agent/main.py ml --market sha        # 沪A
python src/app/stock_agent/main.py ml --market sza        # 深A
python src/app/stock_agent/main.py ml --market all        # 全部A股

# 自定义参数
python src/app/stock_agent/main.py ml --start 20200101 --end 20241231 --threshold 0.45 --capital 100000 --position 0.1
```

**参数说明**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--start` | 20200101 | 回测开始日期 |
| `--end` | 20241231 | 回测结束日期 |
| `--threshold` | 0.45 | ML信号质量阈值 |
| `--capital` | 100000 | 初始资金 |
| `--position` | 0.1 | 单次交易仓位比例 |
| `--market` | gem | 市场类型：gem/sha/sza/all |
| `--output` | outputs | 输出目录 |

### 2. 每日选股扫描

```bash
# 扫描今天（默认创业板）
python src/app/stock_agent/main.py ml-scan

# 指定市场扫描
python src/app/stock_agent/main.py ml-scan --market all   # 全部A股

# 扫描指定日期
python src/app/stock_agent/main.py ml-scan --date 20240429 --threshold 0.50
```

**参数说明**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--date` | 今天 | 扫描日期 |
| `--threshold` | 0.45 | ML信号质量阈值 |
| `--min-gap` | 10 | 最小信号间隔天数 |
| `--max-gap` | 30 | 最大信号间隔天数 |
| `--market` | gem | 市场类型：gem/sha/sza/all |

### 3. 模型训练

```bash
# 训练模型（默认创业板）
python src/app/stock_agent/main.py ml-train

# 指定时间范围和市场
python src/app/stock_agent/main.py ml-train --start 20200101 --end 20241231 --market all

# 自定义模型参数
python src/app/stock_agent/main.py ml-train --n-estimators 200 --max-depth 6 --learning-rate 0.05
```

**参数说明**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--start` | 20200101 | 训练数据开始日期 |
| `--end` | 今天 | 训练数据结束日期 |
| `--market` | gem | 市场类型：gem/sha/sza/all |
| `--n-estimators` | 100 | 决策树数量 |
| `--max-depth` | 5 | 树的最大深度 |
| `--learning-rate` | 0.1 | 学习率 |

### 4. 模型评估

```bash
# 评估当前模型性能
python src/app/stock_agent/main.py ml-eval
```

输出当前模型的准确率、AUC分数，并判断是否需要重训练。

### 5. 模型信息

```bash
# 查看模型详细信息
python src/app/stock_agent/main.py ml-info
```

显示模型路径、训练日期、特征列表和训练历史。

### 6. 微信推送

支持五种推送方式：

| 方式 | 说明 | 适用场景 |
|------|------|----------|
| Server酱 | 推荐，简单易用 | 个人使用 |
| 企业微信 | 企业内部使用 | 团队使用 |
| WxPusher | 功能丰富 | 多用户推送 |
| 公众号模板消息 | 需要用户OpenID | 推送给指定用户 |
| **公众号群发** | 推送给所有关注者 | 公众号运营 |

**配置推送**：

```bash
# 配置Server酱（推荐）- 设置主用户
python src/app/stock_agent/main.py wechat-config --method serverchan --sendkey SCTxxxxxxxx

# 添加更多用户（推送多人）
python src/app/stock_agent/main.py wechat-config --add-key SCTyyyyyyyy
python src/app/stock_agent/main.py wechat-config --add-key SCTzzzzzzzz

# 列出所有用户
python src/app/stock_agent/main.py wechat-config --list-keys

# 删除用户
python src/app/stock_agent/main.py wechat-config --remove-key SCTyyyyyyyy

# 测试推送
python src/app/stock_agent/main.py wechat-config --test
```

**公众号群发（推送给所有关注者）**：

```bash
# 配置公众号（只需要AppID和AppSecret）
python src/app/stock_agent/main.py wechat-config --method mp-mass \
    --mp-appid wx1234567890abcdef \
    --mp-secret 你的AppSecret

# 测试群发
python src/app/stock_agent/main.py wechat-config --test
```

**公众号群发要求**：

| 条件 | 说明 |
|------|------|
| 公众号类型 | 认证的服务号 |
| 群发次数 | 每天可群发1次 |
| 推送对象 | 所有关注公众号的用户 |

**公众号配置获取**：

1. 登录 [微信公众平台](https://mp.weixin.qq.com/)
2. 进入 **开发 → 基本配置**
3. 获取 **AppID** 和 **AppSecret**

**使用推送**：

```bash
# 扫描并推送到微信（默认行为）
python src/app/stock_agent/main.py ml-scan

# 扫描但不推送
python src/app/stock_agent/main.py ml-scan --no-push

# 指定市场推送
python src/app/stock_agent/main.py ml-scan --market all
```

**推送效果示例**：

```
【ML选股】2026-05-17 创业板

共选出 3 只股票

| 序号 | 代码 | 信号日期 | 收盘价 | 止损价 | ML分数 | 等级 |
|:----:|:----:|:--------:|:------:|:------:|:------:|:----:|
| 1 | 300001 | 2026-05-16 | 17.85 | 15.20 | 0.6825 | 良好 |
| 2 | 300012 | 2026-05-16 | 20.64 | 18.50 | 0.5234 | 中等 |
| 3 | 300025 | 2026-05-15 | 8.29 | 7.20 | 0.4856 | 一般 |

---
操作建议:
1. 买入价: 次日开盘价
2. 止损价: 按表格执行
3. 止盈: +5%(卖30%) → +10%(卖30%) → +15%(卖40%)
```

**配置文件** (`wechat_config.json`)：

```json
{
  "method": "serverchan",
  "serverchan_sendkey": "SCTxxxxxxxx",
  "serverchan_sendkeys": [
    "SCTyyyyyyyy",
    "SCTzzzzzzzz"
  ],
  "enabled": true,
  "max_stocks": 10
}
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `serverchan_sendkey` | 主用户SendKey |
| `serverchan_sendkeys` | 其他用户SendKey列表 |
| `wework_webhook` | 主企业微信机器人 |
| `wework_webhooks` | 其他机器人列表 |
| `wxpusher_uids` | WxPusher用户ID列表 |

## 输出示例

### 回测结果

```
================================================================================
回测结果
================================================================================

总交易次数: 211
胜率: 90.05%
平均收益: +7.41%
盈亏比: 1.23

总收益: 371.61%
年化收益: 14.62%
最大回撤: 3.96%
最终资金: 471,609元
```

### CSV输出格式

```csv
stock_code,signal_date,strategy1_date,strategy2_date,gap_days,buy_date,buy_price,final_profit,max_profit,hold_days,is_win,trades,ml_score,score_bin
300002,2024-12-09,2024-11-27,2024-12-09,12,2024-12-10,13.5,-10.41%,0.00%,1,False,STOP:-10.41%/1.0,0.6243,"(0.6, 0.7]"
```

## 策略说明

### 双策略信号配对

1. **策略1**: VAR9底部反转信号
2. **策略2**: J线突破信号
3. **配对规则**: 策略1早于策略2，间隔10-30天

### ML信号质量预测

使用GradientBoostingClassifier预测信号质量，特征包括：
- 均线偏离度 (MA5/MA10/MA20)
- 近期收益率 (5日/10日)
- 波动率、成交量比、K线形态、RSI

### 动态止损止盈

- **止损**: 买入价 - 2×ATR
- **移动止损**: 盈利>5%后，最高价回撤8%
- **分批止盈**: +5%卖30%, +10%卖30%, +15%卖40%

## 相关文档

- [使用说明文档](strategies/ML_STRATEGY_MANUAL.md)
- [变更记录](strategies/ML_STRATEGY_CHANGELOG.md)

## 定时重训练

模型支持定时重训练功能，确保模型性能保持最佳状态。

### 重训练触发条件

| 条件 | 触发动作 |
|------|----------|
| 距离上次训练超过90天 | 自动重训练 |
| 模型准确率低于55% | 自动重训练 |

### 代码中使用

```python
from stock_agent.strategies.model_trainer import ModelTrainer, TrainingConfig

# 创建训练器
config = TrainingConfig(
    start_date="20200101",
    retrain_interval_days=90,  # 90天重训练一次
    performance_threshold=0.55  # 准确率低于55%触发重训练
)
trainer = ModelTrainer(config)

# 启动定时重训练（每天凌晨2点检查）
trainer.start_scheduled_training()

# 停止定时重训练
trainer.stop_scheduled_training()
```

## 注意事项

1. 回测结果仅供参考，不构成投资建议
2. 需要通达信本地数据支持
3. ML模型已预训练，位于 `strategies/signal_model.joblib`
