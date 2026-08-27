# 辐射制冷涂层建筑节能计算工具

这是一个只保留“数据输入—自动计算—结果输出”的本地界面。它不要求用户手工修改 IDF。

## 使用条件

- Windows 10/11；
- Python 3.10 或更高版本；
- OpenStudio 3.11.0；
- EnergyPlus 26.1.0；
- 与建筑地点对应的 EPW 气象文件。

工具只使用 Python 标准库，不需要安装 Streamlit、Flask 或其他第三方包。

## 在设备上使用

1. 从 GitHub 克隆或下载本项目；
2. 安装 Python 3.10+、OpenStudio 3.11.0 和 EnergyPlus 26.1.0；
3. 双击 `启动工具.bat`；
4. 首次运行时选择本机的 `openstudio.exe`、`energyplus.exe` 和 EPW 文件；
5. 程序会把本机路径写入不上传到 GitHub 的 `settings.json`。

也可以复制 `settings.example.json` 为 `settings.json`，再按本机安装位置修改。
模型、运行结果、缓存和本机路径均已由 `.gitignore` 排除，克隆后的仓库保持轻量。

## 建筑参数模板接口

项目中的 `templates/建筑参数输入模板.xlsx` 可以直接转换为统一项目数据，
不需要手工编辑 IDF。读取器会把坐标、时间、布尔值和编号列表转换为模型生成所需的数据类型：

```python
from model_builder import (
    convert_excel_to_json,
    load_project_from_excel,
    save_validation_report,
    validate_project,
)

project = load_project_from_excel("templates/建筑参数输入模板.xlsx")
issues = validate_project(project)
save_validation_report(issues, "outputs/建筑参数校验报告.txt")
convert_excel_to_json(
    "templates/建筑参数输入模板.xlsx",
    "outputs/project.json",
)
```

只有校验报告中“错误”为 0 时，项目数据才应进入 IDF 自动生成步骤。

## OpenStudio 模型生成接口

阶段五开始提供统一项目 JSON 到 OSM/IDF 的封装。OpenStudio 的 Windows
Python 启动器不能可靠接收中文命令行路径，因此封装会自动使用临时 ASCII
工作区，完成后再把所有结果复制回中文项目目录：

```python
from model_builder import build_openstudio_model

result = build_openstudio_model(
    project_json="sample_projects/示例办公楼.json",
    openstudio_executable="C:/OpenStudio-3.11.0/bin/openstudio.exe",
    energyplus_executable="C:/EnergyPlusV26-1-0/energyplus.exe",
    output_dir="generated_model",
    weather_file="examples/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw",
)
print(result.compatible_idf_path)
```

阶段五生成楼层、热区、空间几何、材料、围护构造、窗、人员、照明、
设备、渗透、室外新风、运行时间表、温控器和 HVAC，并加入分区尺寸计算、
设计日及逐时制冷负荷/用电输出。传入 EPW 后会自动执行 EnergyPlus 烟雾测试；
存在 Fatal 错误或缺少制冷负荷输出时，生成过程会直接报错。

阶段五的固定输出结构如下（`build_manifest.json` 是额外的机器可读验收清单）：

```text
generated_model/
├─ project.json
├─ baseline.osm
├─ baseline_25_2.idf
├─ baseline.idf
├─ openstudio.log
├─ transition.log
├─ build_manifest.json
├─ geometry_validation.json
└─ smoke_test/
   └─ eplusout.err
```

## 阶段六几何验收

模型保存前会自动执行几何质量检查。默认楼面面积容差为 `0.01 m2`，
坐标匹配容差为 `1e-5 m`，也可以在调用时调整：

```python
result = build_openstudio_model(
    project_json="sample_projects/示例办公楼.json",
    openstudio_executable="C:/OpenStudio-3.11.0/bin/openstudio.exe",
    energyplus_executable="C:/EnergyPlusV26-1-0/energyplus.exe",
    output_dir="generated_model",
    weather_file="examples/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw",
    floor_area_tolerance_m2=0.01,
    coordinate_tolerance_m=1e-5,
)
```

`geometry_validation.json` 会同时比较项目声明面积、热区多边形面积和
OSM 楼面面积，并检查内部表面匹配、重复空间、负面积表面、窗墙边界及
中间楼层屋面边界。任一检查不通过时，模型生成会停止并保留该报告，
不会把存在几何缺陷的模型交给 EnergyPlus 计算。

## 阶段七涂层模型生成

阶段七从项目 JSON 读取启用的涂料方案和目标构造，成对生成基准 IDF 与
涂层 IDF。原始阶段六 IDF 不会被修改，非目标表面和全部几何坐标保持不变：

```python
from model_builder import build_coating_scenario

result = build_coating_scenario(
    project_json="sample_projects/示例办公楼.json",
    baseline_idf="generated_model/baseline.idf",
    output_dir="stage7_coating_model",
    energyplus_executable="C:/EnergyPlusV26-1-0/energyplus.exe",
    weather_file="examples/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw",
)
print(result.coating_idf_path)
```

输出包括 `baseline.idf`、`coating.idf`、`coating_validation.json`、
`stage7_manifest.json` 和双模型 EnergyPlus 验证目录。当前薄膜等效方式仅
允许 100% 表面覆盖；若输入部分覆盖率，程序会明确停止，避免把部分覆盖
错误地等同为整面材料参数。

## 阶段八精确结果

年度能耗不再使用会显示舍入的 GJ 汇总值，而是直接汇总 EnergyPlus SQLite
中天气运行期的 8760/8784 条逐时记录。阶段八会检查基准与涂层时间戳完全
对齐、逐时和与年度值一致、结果无负值且节电率分母有效，并输出：

- `comparison_results.json`；
- `comparison_results.csv`；
- `hourly_cooling_comparison.csv`；
- `stage8_validation.json`。

## 一键流程接口

```python
from model_builder import run_project_workflow

result = run_project_workflow(
    excel_file="sample_projects/示例办公楼.xlsx",
    weather_file="建筑所在地.epw",
    openstudio_executable="C:/OpenStudio-3.11.0/bin/openstudio.exe",
    energyplus_executable="D:/EnergyPlus-26-1-0/energyplus.exe",
    output_root="test_runs",
)
print(result.comparison_json_path)
```

## 启动

双击 `启动工具.bat`。

首次使用依次选择：

1. 填好的 `建筑参数输入模板.xlsx`；
2. 建筑所在地的 `.epw` 文件；
3. 结果目录；
4. 首次确认 `openstudio.exe` 和 `energyplus.exe` 路径；
5. 点击“开始一键计算”。

建筑几何、围护构造、HVAC 和涂料参数均从 Excel 读取，不再要求用户准备或
手工编辑 IDF。

## 工具自动完成的工作

1. 校验 Excel 并转换为项目 JSON；
2. 调用 OpenStudio 自动制作 OSM 和 25.2 IDF；
3. 转换为 EnergyPlus 26.1 IDF；
4. 执行阶段六六项几何验收；
5. 从 Excel 的启用方案生成基准与涂层模型；
6. 验证仅目标构造发生变化，几何和原始基准保持不变；
7. 使用同一 EPW 分别运行基准和涂层全年工况；
8. 从 SQLite 逐时记录精确计算能耗、峰值和节电率；
9. 输出 JSON、汇总 CSV、8760 小时 CSV 及各阶段验收报告。

太阳吸收率由下式自动换算：

```text
太阳吸收率 = 1 - 太阳反射率
```

## 输出目录

```text
test_runs/
└─ run_日期_时间_微秒/
   ├─ input/
   │  ├─ 建筑参数输入.xlsx
   │  └─ project.json
   ├─ generated_model/
   │  ├─ baseline.osm
   │  ├─ baseline.idf
   │  └─ geometry_validation.json
   ├─ coating_model/
   │  ├─ baseline.idf
   │  ├─ coating.idf
   │  ├─ coating_validation.json
   │  └─ smoke_test/
   ├─ results/
   │  ├─ comparison_results.json
   │  ├─ comparison_results.csv
   │  ├─ hourly_cooling_comparison.csv
   │  └─ stage8_validation.json
   └─ workflow_manifest.json
```

每次计算都会新建独立运行目录，不覆盖以前的结果。

## 结果定义

- 制冷主设备用电：`Cooling:Electricity`；
- 制冷系统综合用电：制冷主设备、风机、水泵和排热设备用电之和；
- 建筑总用电：`Electricity:Facility`；
- 制冷峰值：全年气象运行期逐时制冷用电换算的最大小时平均功率；
- 节电率：`(基准值 - 涂层值) / 基准值 × 100%`。

## 当前适用范围

该版本把涂料视为热阻和热容可忽略的薄涂层，适合高太阳反射率、高长波发射率涂料的建筑能耗筛选及工程比较。

当前自动修改支持外层为以下材料类型的构造：

- `Material`；
- `Material:NoMass`。

如果外层为绿化屋面、复杂窗系统、生态屋面或自定义边界模型，工具会停止并提示，不会静默生成错误模型。

对于依赖 8–13 μm 大气窗口的光谱选择性辐射制冷材料，需要在下一阶段加入光谱辐射模型与 EnergyPlus Python API 的逐时间步耦合；本工具当前采用 EnergyPlus 的宽波段长波发射率模型。

测试版使用示例已完成 Excel 到结果的真实端到端验收。示例中的材料、HVAC
和涂料数据仍是演示值，不能直接代表真实建筑；正式研究必须替换为设计图、
设备资料、实际运行记录和涂料检测报告中的参数。
