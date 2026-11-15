# 退款实验技术方案（合成数据）

> 本文为合成实验资料。

## 接口

- POST /refunds：请求字段 order_id、amount、idempotency_key、role；响应 refund_id。

## 数据

- orders 表：paid、refunded 字段，单位为分。
- refunds 表：idempotency_key 唯一约束。
- ledger 表：按回调 event_id 去重记账。

## 状态

- 订单状态：paid → refunded（全额退款后）。

## 流程

- 退款服务在同一数据库事务中更新 orders.refunded 并插入 refunds 记录。

## 规则

- 剩余可退金额 = paid − refunded，超额请求返回 400。

## 依赖

- 支付渠道回调接口，回调事件携带 event_id。

## 回滚

- 代码可回滚到上一构建；refunds 表新增唯一约束可回滚；回滚后核对订单退款金额与记录数。
