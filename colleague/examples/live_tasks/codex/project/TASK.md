修复 solution.py 中的 event_totals(events)。输入是一个list，每项为恰有key与delta两个字段的dict。key必须是1到16个小写ASCII字母(a-z)组成的str；delta必须是Python int，允许负值和零，bool不算int，且-1000<=delta<=1000。按相同key累计delta，返回按key的ASCII顺序排序的新list，每项为{"key":key,"total":累计整数}，累计值为零的key仍保留。输入空列表时返回[]。不得修改输入的list或dict。外层不是list、行不是dict、key不是str、delta不是int时抛TypeError；字段集合不准确、key文本不符合要求或delta超出范围时抛ValueError。结果必须确定且不依赖外部状态。

约束：只能修改solution.py；不得修改公开测试、任务说明或public_cases.json，不得创建其他文件。solution.py只能包含函数定义；禁止import、文件/网络/进程操作、动态代码、global/nonlocal、类、dunder名称或属性。允许常规纯Python运算、循环、推导式、异常，以及len/type/isinstance/list/dict/set/tuple/int/str/bool/range/sorted/enumerate/zip/all/any/min/max/abs/sum/ValueError/TypeError；允许的方法名称仅为append、extend、copy、sort、items、keys、values、get、strip、isascii、isalpha、islower；其他方法不在本次受限验收范围。允许注释和文档字符串。

请依次读取说明、源文件和公开测试，先运行公开检查观察现有失败，修复代码后再次运行公开检查。若失败则继续修正。工具只允许读取本目录指定文件、写solution.py和运行公开检查；不要猜测或访问外部评估文件。
最终只返回JSON：{"task":"codex_event_totals","status":"complete"}，不要Markdown。该声明仅是格式要求，代码结果仍由独立验收检查。
