# 高密度视频加速卡性能测试

采集Seirios板卡、record-mix进程和整机指标，统计录制编码任务吞吐量，并按场景回填Excel。

## 安装与配置

需要Python 3.10或以上、目标服务器SSH权限及FFmpeg/ffprobe。

```powershell
python -m pip install -r requirements.txt
Copy-Item configs/seirios_env.example.json configs/seirios_env.local.json
```

已有本地配置不要覆盖。真实服务器地址、数据库信息、密码和应用密钥只填写在本地配置或环境变量中；本地配置不提交到Git。

仓库包含采集与调度脚本、场景CSV/JSON模板和使用说明。实测Excel、备份、报告、视频和厂商PDF不随仓库上传。

## 准备工作簿

自行准备与被测环境record_id匹配的工作簿，通过`--workbook`或本地JSON的`paths.workbook`指定。工作簿使用`硬编-1080p`、`硬编-720p`、`软编-1080p`、`软编-720p`等sheet；场景行的B列包含录制任务ID和分屏数。详细格式及阶段见[运行说明](硬编自动化测试运行说明.md)。

```powershell
# 查看帮助，不连接服务器
python scripts/run_hard_encode_excel_workflow.py --help
# 只解析本地工作簿，不修改数据库、重启容器或保存Excel
python scripts/run_hard_encode_excel_workflow.py --env-config configs/seirios_env.local.json --workbook "D:\test\recording-workbook.xlsx" --dry-run
```

真实性能执行会更新录制任务状态、重启record-control容器并回填工作簿，运行前应准备独立测试环境。采集结果输出至`reports/`。

场景清单可由CSV离线生成：

```powershell
python scripts/generate_scenario_manifest.py --input-csv scenarios/task_windows_template.csv --output-json scenarios/scenario_manifest.json
```
