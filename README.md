# log-metrics

日志解析与指标聚合骨架。只认 nginx combined，按文件顺序累加，按字符串截取分钟。

## 已知缺口

- 只解析一种格式
- 时间不转 UTC，跨时区会错
- 不用事件时间，乱序直接累加
- 窗口只支持固定分钟，没有滑动
- 解析失败的行被静默丢弃

## 运行

```powershell
python gen_samples.py
python -m logm.cli access.log
python -m pytest tests -q
```
