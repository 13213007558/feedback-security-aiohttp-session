# 加密 Cookie 会话：无状态 vs 逐账号立即登出

本目录是一个**零网络、可控时钟、可直接运行**的验证原型，用来回答一个看似
矛盾的需求：

> 会话既想「完全无服务端状态」，又想「单个账号能立即登出全部设备」。

结论先行：**这两个目标在严格意义上不可同时成立。** 无状态令牌与「可被外部
立即撤销」是一对矛盾——服务端不记住任何东西，就没有任何东西可以在令牌自然
过期之前否定它。要做到「立即、按账号」撤销，必须接受**最小的、按账号的服务
端状态**，或退而接受一个**有界延迟**。

## 运行方式

```bash
# 场景演示（确定性，不需要真实 sleep / 网络 / 端口）
.venv/bin/python prototype/scenarios.py

# 等价的 pytest 自检测试
.venv/bin/python -m pytest -c prototype/pytest.ini prototype -q
```

仅依赖标准库和项目已有的 `cryptography`（Fernet）。`sqlite3` 是标准库，无需
Redis / Memcached / 数据库服务。

## 现有 EncryptedCookieStorage 的三条关键路径

原型 `core.py` 忠实复刻了库代码（已用真实库做过装载/过期等价性校验）：

- **装载** `aiohttp_session/cookie_storage.py:50`：从请求取 Cookie，调用
  `Fernet.decrypt(..., ttl=self.max_age)`（`cookie_storage.py:58`）。解密失败
  或 TTL 过期抛 `InvalidToken`，在 `cookie_storage.py:62` 被吞掉并返回一个
  **全新的空会话**——旧令牌不会报错，只是「变成未登录」。
- **过期（两层）**：第一层是 Fernet 自带的 TTL；第二层在
  `aiohttp_session/__init__.py:51`，`Session.__init__` 比较
  `now - created > max_age`，超期同样清空。注意 `created` 只在**写入**时刷新
  （`__init__.py:122` 的 `__setitem__`），**只读请求不会滑动续期**。
- **保存** `cookie_storage.py:68`：仅当中间件检测到 `session._changed`
  （`__init__.py:195`）才回写；空会话走 `save_cookie("")` 删除 Cookie
  （`cookie_storage.py:71`），非空则重新 Fernet 加密整体下发
  （`cookie_storage.py:77`）。

### 这套机制能 / 不能保证什么

能保证：

- **机密性与完整性**：密钥不泄露时，客户端无法读取或篡改会话内容，伪造的
  Cookie 解密失败后被当成未登录。
- **自包含、水平扩展友好**：服务端只需持有全局 Fernet 密钥，任意实例都能验签，
  无共享会话存储。
- **有界生命周期**：`max_age`/TTL 让令牌在确定时间后必然失效。

不能保证（在不改前提的情况下）：

- **立即撤销**：登出只是在「当前这台设备」的响应里删 Cookie
  （`invalidate()` 后下发空值）。攻击者或另一台设备**保存下来的旧 Cookie 仍然
  有效**，直到 TTL 到期。服务端没有任何记录能拒绝它。
- **按账号批量登出**：Cookie 里没有任何可被服务端「判旧」的账号级标记；全局
  只有一把对称密钥。

## 反例：两个本地客户端 + 可控时钟

`scenarios.py` 用同一个 `Clock`（手动 `advance`，对应「可控时间」）驱动两个
客户端（同一账号的笔记本 / 手机），关键反例：

1. **纯无状态基线（场景 1）**：两台设备登录后，服务端发出「登出全部设备」，
   但没有任何可写的服务端记录；时钟前进 60 秒后，**两个旧 Cookie 依旧被接受**。
   这直接证明「完全无状态 + 立即登出全部设备」不成立。
2. **短有效期（场景 2）**：`max_age=60`，在登出后第 30 秒令牌**仍然有效**；
   直到跨过 60 秒窗口，Fernet TTL 触发 `InvalidToken`，两台设备才同时失效。
   即短 TTL 给的是「**最坏延迟 = TTL**」，不是立即。
3. **全局换钥（场景 5）**：换掉全局 Fernet 密钥后，目标 alice 被挡，但**无关的
   carol 也一起被挡**。所以换全局密钥是「全员登出」，**不是逐账号撤销**，不能
   冒充该能力。

## 三种方案的保证与成本对比

| 方案 | 撤销时机 | 作用粒度 | 服务端状态 | 每次请求成本 | 主要代价 |
|------|----------|----------|------------|--------------|----------|
| 短有效期 max_age | 延迟 ≤ TTL，非立即 | 全体令牌统一到期 | 无 | 纯本地验签 | 窗口内无法止血；TTL 越短越频繁重新登录 |
| 账号代次（generation） | **立即** | **按账号** | 每账号一个递增整数 | 一次按账号 KV/主键查询 | 引入最小状态；代次需随登录写入 Cookie |
| 集中撤销表（deny-list） | 立即 | 可精确到单个会话 | 每个被撤销会话一条记录 | 一次 sid 集合/表查询 | 状态随撤销量增长，需 TTL 清理；登出全部设备要先枚举该账号所有 sid |
| （对照）全局换钥 | 立即 | **全局所有账号** | 仅密钥 | 无 | 误伤所有用户，且密钥分发/双密钥过渡期复杂 |

要点：

- **短 TTL 是无状态世界里唯一的「撤销」手段，但本质是等待，不是撤销。**
- **账号代次**是「立即、按账号、登出全部设备」性价比最高的方案：状态只有
  `account -> generation` 一行；登录时把当前代次写进加密 Cookie，验签时比较
  Cookie 内代次与服务端当前代次，不一致即 `invalidate()`。代次 +1 即让该账号
  **所有**旧令牌同时失效，且天然不影响其他账号（场景 3 验证 carol 不受影响）。
- **集中撤销表**更适合「只踢某一台设备」这类细粒度诉求；做「登出全部设备」反而
  麻烦（要知道该账号的全部 sid），且表会膨胀，需要按 TTL 垃圾回收
  （`policies.py` 的 `SqliteRevocationTable.purge` 预留了过期列）。

## 推荐的低依赖原型（本目录实现）

- `core.py`：`Clock`（可控时钟）、复刻版 `Session` 与
  `EncryptedCookieStorage`，用 Fernet 的 `encrypt_at_time` /
  `decrypt_at_time` 注入时间，保证时间可完全控制。
- `policies.py`：
  - `MemoryGenerationStore` / `SqliteGenerationStore`：账号代次存储，后者用
    **标准库 sqlite3** 持久化，无外部服务；
  - `MemoryRevocationTable` / `SqliteRevocationTable`：集中撤销表；
  - `GenerationCookieStorage` / `DenyListCookieStorage`：在加密 Cookie 传输之
    上叠加一次状态查询；
  - `rotate_global_key`：演示全局换钥为何不等于逐账号撤销。
- `scenarios.py`：六个场景、13 条 `PASS/FAIL` 断言，覆盖上表全部结论。
- `test_scenarios.py`：同样逻辑的 pytest 版本。

### 落地建议

对「低依赖后端 + 立即登出全部设备」，选**账号代次 + sqlite3（或现有 KV/主库里
一张两列小表）**：保留加密 Cookie 的自包含与水平扩展，只在授权路径增加一次
按主键的整数查询。把它当成一个**可降级依赖**——状态存储不可用时可配置成
fail-closed（拒绝敏感操作）或 fail-open（退化为短 TTL 模式），按业务安全等级
取舍。务必保留较短的 `max_age` 作为兜底，这样即使状态层故障，令牌仍有有界寿命。
