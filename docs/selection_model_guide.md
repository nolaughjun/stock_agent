# 选股模型使用指南

## 概述

选股模型整合了因子模型和风险模型，用于对候选股进行综合评分和排序。

## 工作流程

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  因子分析       │ ──▶ │  模型训练       │ ──▶ │  选股应用       │
│  factor-analysis│     │  train-factor   │     │  analysis       │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

## 第一步：因子有效性分析

在训练模型前，先验证因子是否有效：

```bash
# 分析因子与收益的相关性
uv run python .\src\app\stock_agent\main.py factor-analysis --start 20260101 --end 20260605 --market all
```

**输出报告**：`outputs/factor_analysis/factor_analysis_report.md`

**关键指标**：
- **IC值**：因子与收益的相关性，> 0.02 为有效
- **单调性**：因子分组收益是否单调递增，> 0.6 为良好
- **最优区间**：胜率最高的因子值范围

## 第二步：训练模型

使用历史交易数据训练因子模型：

```bash
# 使用逻辑回归（推荐，适合小样本）
uv run python .\src\app\stock_agent\main.py train-factor-model --start 20250101 --end 20261231 --model logistic

# 使用随机森林（适合中等样本）
uv run python .\src\app\stock_agent\main.py train-factor-model --start 20250101 --end 20261231 --model random_forest

# 使用梯度提升（最高准确率，需要大样本）
uv run python .\src\app\stock_agent\main.py train-factor-model --start 20240101 --end 20261231 --model gradient_boosting
```

**输出文件**：
- `outputs/factor_model.joblib` - 因子模型
- `outputs/selection_model.joblib` - 选股模型（自动集成到选股流程）
- `outputs/factor_model_report.md` - 训练报告

## 第三步：使用模型选股

训练完成后，模型会自动加载到选股流程中：

```bash
# 运行选股分析（自动使用训练好的模型）
uv run python .\src\app\stock_agent\main.py analysis --start 20260501 --end 20260605 --market all
```

## 模型评估指标

| 指标 | 说明 | 目标值 |
|------|------|--------|
| **准确率** | 预测正确的比例 | > 60% |
| **AUC** | 模型区分能力 | > 0.65 |
| **精确率** | 预测盈利中实际盈利的比例 | > 65% |
| **召回率** | 实际盈利中被预测出的比例 | > 50% |

## 当前因子

### 新增因子（V4）
| 因子 | 权重 | 最优区间 | 说明 |
|------|------|----------|------|
| 成交量变化率 | 15% | 缩量或适度放量 | 流动性因子 |
| 换手率 | 15% | 3%-15% | 活跃度因子 |
| 波动率 | 10% | 4%-8% | 风险因子 |

### 原有因子
| 因子 | 权重 | 最优区间 | 说明 |
|------|------|----------|------|
| 连板数 | 20% | 2-3板 | 动量因子 |
| 封板强度 | 15% | > 97% | 技术因子 |
| 市值 | 10% | 20-100亿 | 规模因子 |
| 情绪分 | 15% | > 60 | 情绪因子 |

## 基于因子分析结果的优化

根据因子分析报告，已对评分规则进行优化：

### 成交量变化率
```
原规则：放量50%-150%加分
新规则：缩量（<0%）加分，因为Q1组胜率最高67.2%
```

### 换手率
```
原规则：3%-10%加分
新规则：3%-15%加分，因为Q2-Q3区间胜率最高
```

### 波动率
```
原规则：2%-5%加分
新规则：4%-8%加分，因为Q4区间胜率最高
```

## 注意事项

1. **样本量要求**
   - 最少样本：100笔交易
   - 推荐样本：500笔以上
   - 理想样本：1000笔以上

2. **数据周期**
   - 训练数据应覆盖不同市场环境（牛市/熊市/震荡市）
   - 建议使用至少1年的历史数据

3. **模型更新**
   - 每月重新训练一次模型
   - 当市场环境发生重大变化时，重新训练

4. **过拟合风险**
   - 如果训练准确率>90%但测试准确率<60%，说明过拟合
   - 解决方法：减少特征、增加正则化、增加数据量

## 示例完整流程

```bash
# 1. 因子分析
uv run python .\src\app\stock_agent\main.py factor-analysis --start 20250101 --end 20260605 --market all

# 2. 训练模型
uv run python .\src\app\stock_agent\main.py train-factor-model --start 20250101 --end 20260605 --model logistic

# 3. 验证模型效果
uv run python .\src\app\stock_agent\main.py analysis --start 20260501 --end 20260605 --market all

# 4. 查看报告
cat outputs/yearly/analysis/analysis_all_20260501_20260605.md
```
