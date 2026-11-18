# 企业 ESG 授权门户的异步导出采集

这是围绕城市低碳公开资料的本地研发项目。`source` 提供授权合成来源、资料和来源契约；`collector`
是资料收集端，用真实 Chromium 只通过可见页面与页面触发的下载完成选择、等待、下载与交接。
来源不会替收集端作身份、覆盖或交付判断。

## 运行来源

使用 Python 3.11 或 3.12，先执行 python -m pip install -r requirements.txt。浏览器环境执行 python -m playwright install chromium。

启动命令：python -m source.server --port 8765。浏览器或 HTTP 客户端访问 http://127.0.0.1:8765。接口和资料定义见 docs/source-contract.md。

## 运行采集端

采集端不调用任何批量接口，也不使用 `/__admin/*` 控制入口读取或判断数据；这些入口只用于操作员布置演练情形。

```
# 1) 登记一项资料需求（企业代码 + 报告期 + 披露范围，名称仅作备注）
python -m collector.cli add ./work C02 2025 单体 --label 同名实业

# 2) 用真实 Chromium 按状态机运行（可重复执行，支持中途接手）
python -m collector.cli run ./work --base-url http://127.0.0.1:8765

# 3) 另一终端请求安全暂停：当前需求处理完成后停下，状态已逐需求落盘
python -m collector.cli pause ./work

# 4) 查看状态 / 重新生成交接单
python -m collector.cli status ./work
python -m collector.cli handover ./work
```

安全纪律与状态机说明见 docs/collector-design.md。要点：上下文卡片三项确认前不消耗额度；
创建结果不确定时停留待核实、绝不重复提交，恢复后先按企业代码/报告期/范围三元组核对申请记录，
不认领操作员手工申请；下载地址过期只从同一申请重新领取；只有门户文件集合与已保存原件相符才报告收齐。

工作目录 `./work` 下的 `state.json`、`originals/`、`evidence/`、`handover/` 均为运行产物，已在 .gitignore 忽略。

## 检查来源与采集端

执行 python -m unittest discover -s tests -v。这些检查用真实 Chromium 驱动本地来源，
覆盖正常收齐、断连不重复扣额、手工申请保留、范围混淆拦截、地址过期重领、缺册部分交接、暂停与进程接手。

## 容器

执行 docker build -t esg-export-desk .，然后 docker run --rm -p 8765:8765 esg-export-desk。测试可用 docker run --rm esg-export-desk python -m unittest discover -s tests -v。

source 提供来源服务，data 保存合成原始资料，tests 核对来源行为；docs 记录字段与交互契约。不要将运行日志、凭据或采集数据提交到版本库。
