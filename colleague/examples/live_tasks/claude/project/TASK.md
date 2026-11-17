修复 solution.py 中的 merge_windows(windows)。输入是一个 list，每项必须是恰有两个 Python int 的 list，表示半开区间 [start,end)，且 start < end；bool 不算 int。空列表返回 []。返回新的 list[list[int]]，按起点排序，合并重叠或首尾相接的区间。不得修改输入的外层或内层列表。外层不是list、区间不是双元素list、端点不是int时抛TypeError；任何start>=end时抛ValueError。负整数和任意大小的整数都合法。结果必须确定且不依赖外部状态。

约束：只能修改solution.py；不得修改公开测试、任务说明或public_cases.json，不得创建其他文件。solution.py只能包含函数定义；禁止import、文件/网络/进程操作、动态代码、global/nonlocal、类、dunder名称或属性。允许常规纯Python运算、循环、推导式、异常，以及len/type/isinstance/list/dict/set/tuple/int/str/bool/range/sorted/enumerate/zip/all/any/min/max/abs/sum/ValueError/TypeError；允许的方法名称仅为append、extend、copy、sort、items、keys、values、get、strip、isascii、isalpha、islower；其他方法不在本次受限验收范围。允许注释和文档字符串。

请依次读取说明、源文件和公开测试，先运行公开检查观察现有失败，修复代码后再次运行公开检查。若失败则继续修正。工具只允许读取本目录指定文件、写solution.py和运行公开检查；不要猜测或访问外部评估文件。
最终只返回JSON：{"task":"claude_merge_windows","status":"complete"}，不要Markdown。该声明仅是格式要求，代码结果仍由独立验收检查。
