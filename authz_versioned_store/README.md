# 授权字段带版本编码过渡方案（双服务共用会话存储）

新旧两版服务短期共用一个会话存储，授权字段从 **v1 扁平字符串 scope**
过渡到 **v2 结构化 grant（可带收窄约束）**。本目录是一份可运行的内存模拟，
不依赖外部服务，只用标准库。

## 1. 数据结构与版本编码

所有会话记录统一包一层版本信封（`model.Envelope`）：

```json
{
  "version": 2,
  "revision": 7,
  "payload": { "...": "版本相关的授权文档" },
  "unknown": { "vendor_hint": "..." }
}
```

- `version`：模式版本，读取与写入路径都显式判断。
- `revision`：每次成功 CAS 写入自增，是乐观锁令牌。
- `payload`：
  - v1：`{"scopes": ["repo:read", "repo:write"]}`
  - v2：`{"grants": [{"action", "resource", "constraints"?}]}`
- `unknown`：当前模式不认识的字段一律原样保留在独立桶里，**永不参与授权判定**。

v2 的 `constraints` 只能收窄授权（时间窗 `TimeWindow`、来源 IP 集合等），
不可能扩大授权；判定时上下文缺键即拒绝（fail-closed）。

## 2. 旧版（v1）可读范围——明确边界

| 存储中的版本 | v1 适配器行为 |
|---|---|
| v1 | 原生读取 |
| v2，且全部 grant 都是**无条件**的 | 经无损降级路径读取（scope 与 grant 一一对应） |
| v2，含**任何带约束的 grant** | 抛 `DowngradeNotLosslessError`，**拒绝降级，不截断** |
| v3+（未来版本） | 抛 `IncompatibleRecordError`，不猜含义 |

关键规则在 `codecs.downgrade_v2_to_v1`：只要存在无法用 v1 表达的约束型
grant，整个降级直接失败。把带约束的 grant 拍平成裸 scope 会**删掉约束、
扩大权限**（例如 9:00–17:00 才允许的写会变成全天可写），因此宁可拒绝。

## 3. 升级方向为什么天然安全

v1 → v2 只是把每个 scope 映射成一个无条件 `Grant`，权限集合严格相等
（迁移器 dry-run 会断言 `v1_permissions(scopes) == v2_permissions(grants)`），
不新增任何 `(resource, action)`，也不产生约束。

## 4. 两个独立存储适配器

`adapters.py` 中 `V1Adapter` 与 `V2Adapter` 各自持有独立的编解码、
unknown 策略与可读范围，唯一共享物是构造时传入的同一个
`SharedMemoryStore`（真实环境对应同一个 Redis/DB，只需实现同样的
`load` / `cas` 契约）。

两道独立的写保护（纵深防御）：

1. **版本倒退守卫**：保存前若发现存储版本高于待写版本，抛
   `VersionRollbackError`，不发起写入。
2. **CAS 修订号**：保存必须回传加载时拿到的 `revision`，并发改动会让
   `SharedMemoryStore.cas` 抛 `StaleWriteError`。旧进程的迟到保存因此
   不可能覆盖新版本记录。

## 5. 迁移的原子性（`migration.Migrator`）

- 先做整批 **dry-run**：逐条加载、解码、转换，任何一条失败（未知字段、
  结构损坏、权限集合变化）就抛 `PartialMigrationError`，**一条都不提交**。
- 提交阶段按 dry-run 读到的 revision 做 CAS；并发写入导致的冲突同样
  报 `PartialMigrationError` 并指出哪些已提交，调用方用当前 revision
  重跑即可，已提交的 v2 记录本身权限不增。

## 6. 不变量：迁移期间每次请求都不得扩权

`checks.no_escalation` / `model.decision_subset_of_original` 用多组请求
上下文（空上下文、时间窗内外、不同来源 IP）做探针：服务视图允许的每个
请求，原始授权也必须允许。测试与模拟脚本在每一步都重跑该检查，
`escalation_examples` 还能列出具体的越权三元组作为证据。

## 7. 运行方式

```bash
# 端到端场景演示（打印每步 PASS/FAIL）
python -m authz_versioned_store.simulate

# 14 项不变量测试
python -m pytest authz_versioned_store/tests -q
```

## 8. 场景与测试对照

| 风险场景 | 测试 |
|---|---|
| 版本倒退 | `test_v1_cannot_overwrite_v2_record`、`test_future_version_is_unreadable_by_both_adapters` |
| 未知字段 | `test_unknown_field_strict_reader_rejects_but_data_is_preserved`、`test_envelope_level_unknown_bucket_round_trips` |
| 无损/有损降级 | `test_v2_unconditional_grants_downgrade_losslessly`、`test_constrained_grant_refuses_downgrade_instead_of_truncating` |
| 部分迁移失败 | `test_batch_dry_run_aborts_entire_migration_on_one_bad_record`、`test_commit_phase_conflict_does_not_overwrite_concurrent_save` |
| 旧进程迟到保存 | `test_late_old_process_save_blocked_by_cas_then_accepted_as_fresh_update`、`test_cas_rejects_stale_revision_even_without_version_change` |
| 全程不扩权 | `test_interleaved_v1_v2_round_trip_preserves_permissions`、`test_never_more_permissions_than_original_across_requests` |
