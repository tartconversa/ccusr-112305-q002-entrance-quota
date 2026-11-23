# 雪场出入口营运资料

经营记录包括分向闸机计数、人工点验、通道状态、团体票和区域售票终端回传。全部是合成数据，资料见 [经营口径](data/README.md)。

使用 Python 3.11 和标准库。从项目根目录查看来源概要与关系。

~~~sh
python3 scripts/inspect_sources.py
python3 -m unittest discover -s tests
~~~
