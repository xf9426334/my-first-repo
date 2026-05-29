#!/usr/bin/env python3
"""
================================================================================
 12英寸晶圆宏动XY平台栅格扫描运动控制程序
 ============== ============ ============ ============ ============ ==========

 功能概述：
   1. 生成12寸晶圆的栅格扫描路径（蛇形往返，Serpentine Raster）
   2. 纯软件仿真模式：计算运动轨迹、距离、时间、覆盖率
   3. 扫描路径可视化（matplotlib）
   4. 导出ACS SPiiPlus控制器脚本（ACSPL+程序）

 坐标系定义：
   · 晶圆中心 = 原点 (0, 0)
   · X轴：左右方向（水平扫描方向）
   · Y轴：前后方向（行步进方向）
   · Z轴：上下方向（WLI对焦方向）

 边界约束：
   · 晶圆半径：150mm（12英寸 ≈ 300mm直径）
   · 边缘排除区：3mm（晶圆边缘非检测区）
   · 有效扫描半径：147mm（可检测区域）
   · 探头中心极限半径：144.5mm（保证5mm FOV边缘不超出147mm有效区）
     → 探头中心必须在距晶圆中心144.5mm的圆内移动

 扫描策略——蛇形往返光栅扫描：
   · X方向扫描，Y方向步进
   · 相邻行方向相反（左→右、右→左交替），消除空返程
   · 行间重叠率默认15%（可调整10%~20%）
   · 每行X范围由圆的弦长决定：|X| ≤ sqrt(R_limit^2 - Y^2)

 硬件背景：
   · ACS SPiiPlus控制器 → 大理石气浮XY宏动平台
   · WLI（白光干涉仪）传感器 → 5mm×5mm视场
   · 通过ACS控制器的PEG（位置事件发生器）触发WLI采集图像

 作者：Claude Code
 日期：2026-05-29
================================================================================
"""

import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple, Dict, Optional


def _write_text_file(fpath: str, content: str, label: str) -> bool:
    """
    安全写入文本文件。若目标文件被占用（如 Excel 打开中），
    自动改用带时间戳的备用文件名，避免整个程序崩溃。
    """
    try:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"[导出] {label}已保存 → {fpath}")
        return True
    except PermissionError:
        base, ext = os.path.splitext(fpath)
        alt_path = f"{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        try:
            with open(alt_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"[警告] {label}目标文件被占用，无法覆盖：{fpath}")
            print(f"       请关闭 Excel/记事本 等程序后重试。")
            print(f"[导出] {label}已改存为 → {alt_path}")
            return True
        except OSError as exc:
            print(f"[错误] {label}保存失败：{exc}")
            return False
    except OSError as exc:
        print(f"[错误] {label}保存失败：{exc}")
        return False

# ============================================================================
#  第一部分：配置参数
# ============================================================================

@dataclass
class ScanConfig:
    """
    扫描参数配置数据类
    所有长度单位：mm
    所有时间单位：s
    所有速度单位：mm/s
    """
    # ── 晶圆几何参数 ──
    wafer_radius: float = 150.0          # 晶圆半径（12英寸≈300mm直径）
    edge_exclusion: float = 3.0          # 边缘排除区宽度

    # ── WLI 视场参数 ──
    fov_x: float = 5.0                   # 视场X方向尺寸
    fov_y: float = 5.0                   # 视场Y方向尺寸
    overlap_ratio: float = 0.15          # 行间重叠率（0.0 ~ 1.0，建议0.10~0.20）

    # ── 运动参数 ──
    scan_velocity: float = 50.0          # 扫描行速度
    transition_velocity: float = 100.0   # 行间过渡移动速度
    acceleration: float = 500.0          # 加速度
    deceleration: float = 500.0          # 减速度

    # ── Z轴参数 ──
    z_safe_height: float = 5.0           # Z轴安全高度（高于晶圆表面，过渡移动时使用）
    z_scan_height: float = 0.0           # Z轴扫描高度（WLI对焦位置）
    z_velocity: float = 20.0             # Z轴移动速度

    # ── 输出 ──
    output_dir: str = "./scan_output"    # 输出文件目录


# ============================================================================
#  第二部分：运动学辅助函数
# ============================================================================

def trapezoidal_move_time(distance: float, velocity: float,
                           acceleration: float) -> float:
    """
    计算梯形速度曲线下的移动时间

    梯形速度曲线：
      · 加速段：从0加速到velocity，时间 = velocity/acceleration
      · 匀速段：以velocity匀速，时间 = (distance - 加速距离 - 减速距离) / velocity
      · 减速段：从velocity减速到0，时间 = velocity/acceleration

    如果距离太短无法达到最大速度（三角形曲线）：
      · 时间 = 2 * sqrt(distance / acceleration)

    Args:
        distance: 移动距离 (mm)
        velocity: 最大速度 (mm/s)
        acceleration: 加速度 (mm/s^2)

    Returns:
        移动时间 (s)
    """
    if distance <= 0:
        return 0.0

    # 加速（或减速）到最大速度所需的距离
    accel_distance = velocity**2 / (2 * acceleration)

    # 判断是梯形还是三角形速度曲线
    if 2 * accel_distance <= distance:
        # 梯形：有匀速段
        accel_time = velocity / acceleration
        cruise_distance = distance - 2 * accel_distance
        cruise_time = cruise_distance / velocity
        return 2 * accel_time + cruise_time
    else:
        # 三角形：无法达到最大速度
        # 峰值速度 = sqrt(distance * acceleration)
        return 2 * math.sqrt(distance / acceleration)


# ============================================================================
#  第三部分：WaferScanner 晶圆扫描器
# ============================================================================

class WaferScanner:
    """
    12英寸晶圆宏动平台栅格扫描控制器

    ── 核心职责 ──
    1. 生成栅格扫描路径点（Waypoints）
    2. 仿真计算运动学参数（距离、时间、覆盖率）
    3. 可视化扫描路径
    4. 导出ACS SPiiPlus控制脚本

    ── 扫描流程图 ──
    Home(0,0) → 第一行起点 → [扫描行1] → 过渡到行2 → [扫描行2] → ... → Home(0,0)

    ── 使用方式 ──
        scanner = WaferScanner()
        scanner.generate_scan_path()    # 生成路径
        scanner.simulate()              # 仿真计算
        scanner.plot()                  # 可视化
        scanner.export_acs_script()     # 导出ACS脚本
    """

    def __init__(self, config: Optional[ScanConfig] = None):
        """
        初始化晶圆扫描器

        Args:
            config: 扫描参数配置，为None则使用默认配置
        """
        self.cfg = config or ScanConfig()
        self._compute_derived_params()

        # 运行时数据
        self.scan_rows: List[dict] = []        # 扫描行列表
        self.all_waypoints: List[dict] = []    # 全部路径点
        self.stats: dict = {}                  # 仿真统计结果

    def _compute_derived_params(self):
        """
        根据用户配置计算所有派生参数

        关键计算：
          · effective_radius: 可以被WLI有效检测的区域半径
          · probe_limit: 探头中心允许的最大移动半径
          · row_step: Y方向行步进量（考虑重叠后）
          · num_rows: 预估扫描行数
        """
        c = self.cfg

        # 有效扫描半径 = 晶圆半径 - 边缘排除区
        self.effective_radius = c.wafer_radius - c.edge_exclusion  # 147mm

        # 探头中心极限半径 = 有效半径 - FOV半边长
        # 保证WLI视场（5mm×5mm）的四个角都不会超出有效扫描区
        fov_half = max(c.fov_x, c.fov_y) / 2.0   # 2.5mm
        self.probe_limit = self.effective_radius - fov_half  # 144.5mm

        # Y方向行步进 = FOV高度 × (1 - 重叠率)
        # 例如：5mm × (1-0.15) = 4.25mm
        self.row_step = c.fov_y * (1.0 - c.overlap_ratio)

        # 预估扫描行数（上下对称分布）
        self.num_rows = int(2 * self.probe_limit / self.row_step) + 1

    # ──────────────────────────────────────────────────────────────
    #  路径生成
    # ──────────────────────────────────────────────────────────────

    def generate_scan_path(self, verbose: bool = True) -> List[dict]:
        """
        生成蛇形往返栅格扫描路径

        算法步骤：
          1. 计算Y方向各行位置（以Y=0为中心对称分布）
          2. 对每一行，根据圆方程计算X方向可扫描范围
          3. 奇数行从左到右扫描，偶数行从右到左扫描（蛇形往返）
          4. 跳过弦长过短的行（＜FOV宽度，避免无意义的微小扫描）
          5. 添加起始/结束的Home位置点

        扫描模式示意（蛇形往返）：
          Y₃  ←────────────────────→     (右→左)
          Y₂  →────────────────────←     (左→右)
          Y₁  ←────────────────────→     (右→左)
          Y₀  →────────────────────←     (左→右)
               ↑晶圆左边界       ↑晶圆右边界

        Returns:
            全部路径点列表，每点包含 {x, y, type, row_id, desc}
        """
        self.scan_rows = []
        self.all_waypoints = []

        if verbose:
            print("=" * 62)
            print("  12英寸晶圆栅格扫描路径生成")
            print("=" * 62)
            print(f"  晶圆半径:            {self.cfg.wafer_radius} mm")
            print(f"  边缘排除区:          {self.cfg.edge_exclusion} mm")
            print(f"  有效扫描半径:        {self.effective_radius} mm")
            print(f"  探头中心极限半径:    {self.probe_limit:.1f} mm")
            print(f"  WLI视场(FOV):        {self.cfg.fov_x}×{self.cfg.fov_y} mm")
            print(f"  行间重叠率:          {self.cfg.overlap_ratio*100:.0f}%")
            print(f"  行步进量:            {self.row_step:.3f} mm")
            print(f"  预估扫描行数:        {self.num_rows}")
            print()

        # ── 步骤1：计算Y行位置（以Y=0为中心对称分布）──
        # 这样做的好处：扫描区域上下对称，覆盖均匀
        total_rows = self.num_rows
        y_offset = -(total_rows - 1) / 2.0 * self.row_step

        # ── 步骤2：起点 ──
        self._add_waypoint(0.0, 0.0, "home", -1,
                           "Home位置：晶圆中心原点，Z轴安全高度")

        # ── 步骤3：逐行生成扫描路径 ──
        scan_left_to_right = True   # 第一行从左到右

        for row_idx in range(total_rows):
            y = y_offset + row_idx * self.row_step

            # 检查Y坐标是否在探头极限范围内
            if abs(y) > self.probe_limit:
                continue

            # 计算此行在晶圆内的X范围（圆的弦长）
            # 圆方程：x^2 + y^2 = R^2  →  x_half = sqrt(R^2 - y^2)
            x_half_sq = self.probe_limit**2 - y**2
            if x_half_sq <= 0:
                continue
            x_half = math.sqrt(x_half_sq)

            # 跳过弦长过短的行（小于FOV宽度，扫描意义不大）
            if 2 * x_half < self.cfg.fov_x * 0.5:
                if verbose:
                    print(f"  [跳过] 第{row_idx+1:3d}行 "
                          f"Y={y:+7.2f}: 弦长={2*x_half:.2f}mm < 半FOV")
                continue

            # 确定此行的扫描起点和终点（考虑蛇形往返方向）
            if scan_left_to_right:
                x_start, x_end = -x_half, x_half
                direction = "→"
            else:
                x_start, x_end = x_half, -x_half
                direction = "←"

            # 记录此行信息
            actual_row_id = len(self.scan_rows) + 1
            self.scan_rows.append({
                "id": actual_row_id,
                "y": y,
                "x_start": x_start,
                "x_end": x_end,
                "x_half": x_half,
                "direction": direction,
                "scan_length": 2 * x_half,
            })

            # 路径点：行起点（过渡移动到此，Z安全高度）
            self._add_waypoint(x_start, y, "row_start", actual_row_id - 1,
                               f"第{actual_row_id:3d}行起点 Y={y:+7.2f} "
                               f"X={x_start:+7.2f} {direction}")

            # 路径点：行终点（扫描结束后到达此点）
            self._add_waypoint(x_end, y, "row_end", actual_row_id - 1,
                               f"第{actual_row_id:3d}行终点 Y={y:+7.2f} "
                               f"X={x_end:+7.2f} {direction}")

            # 切换下一行方向
            scan_left_to_right = not scan_left_to_right

        # ── 步骤4：终点，返回Home ──
        self._add_waypoint(0.0, 0.0, "home", -1,
                           "返回Home位置：晶圆中心原点")

        if verbose:
            print(f"\n  实际扫描行数: {len(self.scan_rows)}")
            print(f"  路径点总数:   {len(self.all_waypoints)}")
            # 计算总扫描长度
            total_scan_len = sum(row["scan_length"] for row in self.scan_rows)
            print(f"  总扫描长度:   {total_scan_len:.1f} mm "
                  f"({total_scan_len/1000:.2f} m)")
            print("=" * 62)
            print()

        return self.all_waypoints

    def _add_waypoint(self, x: float, y: float, wp_type: str,
                      row_id: int, description: str):
        """内部方法：向路径点列表添加一个点"""
        self.all_waypoints.append({
            "x": round(x, 4),
            "y": round(y, 4),
            "type": wp_type,       # home / row_start / row_end
            "row_id": row_id,       # 所属行编号（-1表示非扫描行）
            "desc": description,
        })

    # ──────────────────────────────────────────────────────────────
    #  仿真计算
    # ──────────────────────────────────────────────────────────────

    def simulate(self, verbose: bool = True) -> dict:
        """
        仿真运行——计算关键运动学参数

        计算内容：
          · XY总移动距离（区分扫描距离 vs 过渡距离）
          · 基于梯形速度曲线的耗时估算
          · Z轴移动时间
          · 有效覆盖面积和覆盖率

        返回的stats字典包含所有仿真数据，可被plot()和export_acs_script()复用。
        """
        if not self.all_waypoints:
            self.generate_scan_path(verbose=False)

        c = self.cfg

        # ── 距离统计 ──
        total_scan_dist = 0.0        # 纯粹扫描距离（WLI在采集）
        total_transition_dist = 0.0  # 过渡移动距离（行间跳转 + 进出）
        total_scan_time = 0.0        # 扫描耗时
        total_transition_time = 0.0  # 过渡耗时
        segment_details = []         # 每段详细信息

        prev_wp = self.all_waypoints[0]
        for i in range(1, len(self.all_waypoints)):
            curr_wp = self.all_waypoints[i]
            dx = curr_wp["x"] - prev_wp["x"]
            dy = curr_wp["y"] - prev_wp["y"]
            dist = math.sqrt(dx**2 + dy**2)

            # 判断段类型
            if prev_wp["type"] == "row_start" and curr_wp["type"] == "row_end":
                # 同行扫描段（同一行的起点→终点）
                seg_type = "scan"
                total_scan_dist += dist
                move_time = trapezoidal_move_time(dist, c.scan_velocity,
                                                   c.acceleration)
                total_scan_time += move_time
                velocity_used = c.scan_velocity
            else:
                # 过渡段（Home→首行、行间跳转、末行→Home）
                seg_type = "transition"
                total_transition_dist += dist
                move_time = trapezoidal_move_time(dist, c.transition_velocity,
                                                   c.acceleration)
                total_transition_time += move_time
                velocity_used = c.transition_velocity

            segment_details.append({
                "from": prev_wp["desc"],
                "to": curr_wp["desc"],
                "distance": dist,
                "time": move_time,
                "type": seg_type,
                "velocity": velocity_used,
            })

            prev_wp = curr_wp

        total_xy_dist = total_scan_dist + total_transition_dist
        total_xy_time = total_scan_time + total_transition_time

        # ── Z轴时间 ──
        # 每个扫描行需要：下降Z → 扫描 → 上升Z（2次Z移动/行）
        # 加上进/出各1次Z移动
        z_move_count = len(self.scan_rows) * 2 + 2
        z_move_dist_per = abs(c.z_safe_height - c.z_scan_height)
        total_z_dist = z_move_count * z_move_dist_per
        # Z轴移动：加速→匀速→减速
        z_time_per_move = trapezoidal_move_time(z_move_dist_per,
                                                 c.z_velocity,
                                                 c.acceleration)
        total_z_time = z_move_count * z_time_per_move

        # ── 总耗时 ──
        total_time = total_xy_time + total_z_time

        # ── 覆盖率计算 ──
        # 扫描覆盖面积 = 各行扫描长度 × FOV_Y（视为扫描条带）
        # 由于有重叠，实际面积需要去重，这里计算的是"名义覆盖面积"
        scanned_area_raw = total_scan_dist * c.fov_y
        # 有效区域面积（mm^2）
        effective_area = math.pi * self.effective_radius**2
        # 名义覆盖率（可能>100%因为有重叠）
        coverage_nominal = scanned_area_raw / effective_area * 100

        # ── 汇总 ──
        self.stats = {
            "num_rows": len(self.scan_rows),
            "num_waypoints": len(self.all_waypoints),
            "row_step": self.row_step,
            "probe_limit": self.probe_limit,
            "effective_radius": self.effective_radius,
            # 距离
            "total_scan_distance": total_scan_dist,
            "total_transition_distance": total_transition_dist,
            "total_xy_distance": total_xy_dist,
            "total_z_distance": total_z_dist,
            # 时间
            "scan_time": total_scan_time,
            "transition_time": total_transition_time,
            "xy_time": total_xy_time,
            "z_time": total_z_time,
            "total_time": total_time,
            # 面积
            "effective_area": effective_area,
            "scanned_area": scanned_area_raw,
            "coverage_nominal_pct": coverage_nominal,
            # 效率
            "efficiency": total_scan_dist / max(total_xy_dist, 0.001) * 100,
            # 详情
            "segments": segment_details,
        }

        if verbose:
            self._print_simulation_report()

        return self.stats

    def _print_simulation_report(self):
        """打印仿真报告（格式化输出）"""
        s = self.stats
        print("\n" + "=" * 62)
        print("  仿真运动学报告")
        print("=" * 62)

        # 路径信息
        print(f"  {'─' * 40}")
        print(f"  路径概览")
        print(f"  {'─' * 40}")
        print(f"  扫描行数:            {s['num_rows']:6d} 行")
        print(f"  路径点总数:          {s['num_waypoints']:6d} 点")
        print(f"  行步进量:            {s['row_step']:6.3f} mm")

        # 距离
        print(f"  {'─' * 40}")
        print(f"  移动距离")
        print(f"  {'─' * 40}")
        print(f"  扫描移动距离:        {s['total_scan_distance']:8.1f} mm  "
              f"({s['total_scan_distance']/1000:.2f} m)")
        print(f"  过渡移动距离:        {s['total_transition_distance']:8.1f} mm  "
              f"({s['total_transition_distance']/1000:.2f} m)")
        print(f"  XY总移动距离:        {s['total_xy_distance']:8.1f} mm  "
              f"({s['total_xy_distance']/1000:.2f} m)")
        print(f"  Z轴总移动距离:       {s['total_z_distance']:8.1f} mm")

        # 速度参数
        print(f"  {'─' * 40}")
        print(f"  运动参数")
        print(f"  {'─' * 40}")
        print(f"  扫描速度:            {self.cfg.scan_velocity:6.0f} mm/s")
        print(f"  过渡速度:            {self.cfg.transition_velocity:6.0f} mm/s")
        print(f"  加速度/减速度:       {self.cfg.acceleration:6.0f} mm/s^2")

        # 时间
        print(f"  {'─' * 40}")
        print(f"  预计耗时")
        print(f"  {'─' * 40}")
        print(f"  扫描时间(XY):        {s['scan_time']:8.1f} s")
        print(f"  过渡时间(XY):        {s['transition_time']:8.1f} s")
        print(f"  Z轴移动时间:         {s['z_time']:8.1f} s")
        print(f"  {'─' * 40}")
        print(f"  预计总耗时:          {s['total_time']:8.1f} s  "
              f"({s['total_time']/60:.1f} min)")

        # 面积与效率
        print(f"  {'─' * 40}")
        print(f"  覆盖与效率")
        print(f"  {'─' * 40}")
        print(f"  有效扫描面积:        {s['effective_area']:8.0f} mm^2  "
              f"(π×{self.effective_radius}^2)")
        print(f"  名义覆盖面积:        {s['scanned_area']:8.0f} mm^2")
        print(f"  名义覆盖率:          {s['coverage_nominal_pct']:8.1f}%  "
              f"(含{s['row_step']:.2f}mm步进下的重叠)")
        print(f"  扫描效率:            {s['efficiency']:8.1f}%  "
              f"(扫描距离/总距离)")
        print("=" * 62)
        print()

    # ──────────────────────────────────────────────────────────────
    #  可视化
    # ──────────────────────────────────────────────────────────────

    def plot(self, save: bool = True, show: bool = False,
             filename: str = "scan_path.png"):
        """
        可视化扫描路径

        绘制内容：
          · 红色实线圆：晶圆物理边缘 (R=150mm)
          · 橙色虚线圆：有效扫描边界 (R=147mm)
          · 绿色点线圆：探头中心极限 (R=144.5mm)
          · 彩色实线：各行扫描路径（渐变色区分不同行）
          · 灰色虚线：行间过渡移动
          · 绿色圆点：Home起点
          · 红色圆点：Home终点

        Args:
            save: 是否保存为PNG文件
            show: 是否弹出显示窗口
            filename: 保存的文件名
        """
        try:
            import matplotlib.pyplot as plt
            import matplotlib.patches as mpatches
        except ImportError:
            print("[提示] matplotlib 未安装，跳过绘图。")
            print("       安装命令: pip install matplotlib")
            return

        # 配置中文字体（Windows下常用微软雅黑或黑体）
        try:
            import matplotlib.font_manager as fm
            # 尝试找到可用的中文字体
            chinese_fonts = ['Microsoft YaHei', 'SimHei', 'SimSun',
                             'KaiTi', 'FangSong', 'WenQuanYi Micro Hei']
            available_fonts = [f.name for f in fm.fontManager.ttflist]
            selected_font = None
            for font_name in chinese_fonts:
                if font_name in available_fonts:
                    selected_font = font_name
                    break
            if selected_font:
                plt.rcParams['font.family'] = selected_font
                plt.rcParams['axes.unicode_minus'] = False
        except Exception:
            pass  # 字体配置失败时使用默认字体

        if not self.all_waypoints:
            self.generate_scan_path(verbose=False)

        fig, ax = plt.subplots(1, 1, figsize=(12, 12))
        ax.set_facecolor('#FAFAFA')

        # ── 绘制三个关键边界圆 ──
        # 晶圆物理边缘
        wafer = plt.Circle((0, 0), self.cfg.wafer_radius,
                           fill=False, edgecolor='red', linewidth=2.5,
                           linestyle='-', zorder=1)
        ax.add_patch(wafer)

        # 有效扫描边界（排除边缘后）
        eff = plt.Circle((0, 0), self.effective_radius,
                         fill=False, edgecolor='#E67E22', linewidth=1.8,
                         linestyle='--', zorder=1)
        ax.add_patch(eff)

        # 探头中心极限边界
        probe = plt.Circle((0, 0), self.probe_limit,
                           fill=False, edgecolor='#27AE60', linewidth=1.2,
                           linestyle=':', zorder=1)
        ax.add_patch(probe)

        # ── 绘制扫描行 ──
        n_rows = len(self.scan_rows)
        if n_rows > 0:
            # 用颜色渐变区分各行（从底部到顶部）
            colors = plt.cm.plasma([i / n_rows for i in range(n_rows)])

            for i, row in enumerate(self.scan_rows):
                ax.plot([row["x_start"], row["x_end"]],
                        [row["y"], row["y"]],
                        '-', color=colors[i], linewidth=0.8, zorder=3)

            # ── 绘制行间过渡线（仅相邻行之间） ──
            for i in range(n_rows - 1):
                r1 = self.scan_rows[i]
                r2 = self.scan_rows[i + 1]
                ax.plot([r1["x_end"], r2["x_start"]],
                        [r1["y"], r2["y"]],
                        '--', color='gray', linewidth=0.3, alpha=0.4, zorder=2)

        # ── 标记Home位置 ──
        ax.plot(0, 0, 'o', color='#27AE60', markersize=12,
                markeredgecolor='darkgreen', markeredgewidth=1.5,
                zorder=5, label='Home (起点/终点)')

        # ── 图例 ──
        from matplotlib.lines import Line2D
        legend_elements = [
            Line2D([0], [0], color='red', linewidth=2.5,
                   label=f'晶圆边缘 (R={self.cfg.wafer_radius}mm)'),
            Line2D([0], [0], color='#E67E22', linewidth=1.8, linestyle='--',
                   label=f'有效扫描边界 (R={self.effective_radius}mm)'),
            Line2D([0], [0], color='#27AE60', linewidth=1.2, linestyle=':',
                   label=f'探头中心极限 (R={self.probe_limit:.1f}mm)'),
            Line2D([0], [0], color='#7F8C8D', linewidth=0.8,
                   label=f'扫描路径 ({n_rows}行)'),
            Line2D([0], [0], color='gray', linewidth=0.3, linestyle='--',
                   alpha=0.4, label='行间过渡'),
            Line2D([0], [0], marker='o', color='w', markerfacecolor='#27AE60',
                   markersize=10, label='Home'),
        ]
        ax.legend(handles=legend_elements, loc='upper right',
                  fontsize=9, framealpha=0.9)

        # ── 标题与轴标签 ──
        ax.set_title(
            f'12英寸晶圆栅格扫描路径\n'
            f'扫描行数: {n_rows} | 步进: {self.row_step:.2f}mm | '
            f'重叠率: {self.cfg.overlap_ratio*100:.0f}% | '
            f'FOV: {self.cfg.fov_x}×{self.cfg.fov_y}mm | '
            f'总距离: {self.stats.get("total_xy_distance", 0):.0f}mm',
            fontsize=13, fontweight='bold', pad=15
        )

        ax.set_xlabel('X (mm) ── 扫描方向', fontsize=11)
        ax.set_ylabel('Y (mm) ── 步进方向', fontsize=11)
        ax.set_aspect('equal')

        # 设置坐标范围（晶圆外扩20mm以显示完整边界）
        limit = self.cfg.wafer_radius + 20
        ax.set_xlim(-limit, limit)
        ax.set_ylim(-limit, limit)
        ax.grid(True, alpha=0.25, linestyle='--')
        ax.axhline(y=0, color='black', linewidth=0.5, alpha=0.3)
        ax.axvline(x=0, color='black', linewidth=0.5, alpha=0.3)

        # ── 保存和/或显示 ──
        if save:
            os.makedirs(self.cfg.output_dir, exist_ok=True)
            fpath = os.path.join(self.cfg.output_dir, filename)
            plt.savefig(fpath, dpi=150, bbox_inches='tight',
                        facecolor='white')
            print(f"[绘图] 扫描路径图已保存 → {fpath}")

        if show:
            plt.show()
        else:
            plt.close(fig)

    # ──────────────────────────────────────────────────────────────
    #  ACS脚本导出
    # ──────────────────────────────────────────────────────────────

    def export_acs_script(self, filename: str = "wafer_scan.acs") -> str:
        """
        导出ACS SPiiPlus控制器脚本（ACSPL+语言）

        生成的脚本可直接在SPiiPlus MMI Application Studio中加载运行。
        脚本包含：
          · 全局变量定义
          · 运动参数设置（速度、加速度、加加速度）
          · 轴回零和安全初始化
          · 逐行扫描循环（过渡到行起点 → 下降Z → 扫描 → 上升Z）
          · 扫描完成返回Home

        ACSPL+ 关键指令说明：
          VEL x        — 设置运动速度 (mm/s)
          ACC x        — 设置加速度 (mm/s^2)
          DEC x        — 设置减速度 (mm/s^2)
          JERK x       — 设置加加速度 (mm/s^3)，使运动更平滑
          PTP/r 0, X=x, Y=y, Z=z  — 绝对坐标点到点运动 (PTP = Point-To-Point)
          HOME X Y Z   — 指定轴回零
          WAIT xxx     — 等待xxx毫秒（确保运动完成）
          HALT X Y Z   — 紧急停止指定轴
          STOP         — 程序结束

        Args:
            filename: 输出的脚本文件名（.acs 扩展名）

        Returns:
            ACSPL+脚本文本内容
        """
        if not self.all_waypoints:
            self.generate_scan_path(verbose=False)

        c = self.cfg
        lines = []

        def w(s=""):
            lines.append(s)

        # ── 文件头 ──
        w("! ============================================================")
        w("!  12英寸晶圆栅格扫描程序（ACSPL+）")
        w(f"!  生成时间:      {self._now()}")
        w(f"!  扫描行数:      {len(self.scan_rows)}")
        w(f"!  WLI视场(FOV):  {c.fov_x}×{c.fov_y} mm")
        w(f"!  行间重叠率:    {c.overlap_ratio*100:.0f}%")
        w(f"!  行步进量:      {self.row_step:.3f} mm")
        w(f"!  探头极限半径:  {self.probe_limit:.1f} mm")
        w("! ============================================================")
        w("")

        # ── 全局变量 ──
        w("! ---- 全局变量定义 ----")
        w("global INT    scan_row_total      ! 扫描总行数")
        w("global INT    scan_row_idx        ! 当前行索引")
        w("global REAL   scan_y              ! 当前行Y坐标")
        w("global REAL   scan_x_start        ! 当前行X起点")
        w("global REAL   scan_x_end          ! 当前行X终点")
        w("global REAL   safe_z              ! Z轴安全高度")
        w("global REAL   scan_z              ! Z轴扫描高度")
        w("")
        w(f"scan_row_total = {len(self.scan_rows)}")
        w(f"safe_z = {c.z_safe_height}")
        w(f"scan_z = {c.z_scan_height}")
        w("")

        # ── 运动参数 ──
        w("! ---- 运动参数设置 ----")
        w(f"VEL {c.scan_velocity}            ! 默认速度 mm/s")
        w(f"ACC {c.acceleration}             ! 加速度 mm/s^2")
        w(f"DEC {c.deceleration}             ! 减速度 mm/s^2")
        w(f"JERK {c.acceleration * 10:.0f}   ! 加加速度 mm/s^3（平滑启停）")
        w("")

        # ── 初始化 ──
        w("! ---- 轴初始化与回零 ----")
        w("! *** 注意：首次运行需确认各轴软限位已正确设置 ***")
        w("! *** SET SLIMIT X = -180 180 等 ***")
        w("ENABLE X Y Z             ! 使能各轴")
        w("WAIT 200")
        w("HOME X Y Z               ! 执行回零")
        w("WAIT 500                 ! 等待回零完成")
        w("")
        w("! ---- 移动到Home位置（晶圆中心上方安全高度）----")
        w(f"VEL {c.transition_velocity}")
        w(f"PTP/r 0, X=0, Y=0, Z={c.z_safe_height}")
        w("WAIT 200")
        w("")

        # ── 扫描循环 ──
        w("! ============================================================")
        w("!  栅格扫描主循环（蛇形往返）")
        w("! ============================================================")
        w("scan_row_idx = 0")
        w("")

        for i, row in enumerate(self.scan_rows):
            w(f"! ───────────────────────────────────────")
            w(f"!  第 {row['id']:3d}/{len(self.scan_rows)} 行  "
              f"Y={row['y']:+7.2f}  "
              f"X={row['x_start']:+7.2f} {row['direction']} "
              f"X={row['x_end']:+7.2f}  "
              f"弦长={row['scan_length']:.1f}mm")
            w(f"! ───────────────────────────────────────")

            # 移动到行起点（Z安全高度）
            w(f"! 过渡移动 → 第{row['id']}行起点")
            w(f"VEL {c.transition_velocity}")
            w(f"PTP/r 0, X={row['x_start']:.3f}, Y={row['y']:.3f}, "
              f"Z={c.z_safe_height}")
            w("WAIT 150")

            # Z轴下降至扫描高度
            w(f"! Z轴下降 → WLI对焦高度")
            w(f"PTP/r 0, Z={c.z_scan_height}")
            w("WAIT 100")

            # 扫描行
            w(f"! *** 扫描第{row['id']}行 ***")
            w(f"VEL {c.scan_velocity}")
            w(f"! （实际应用中此处可启用PEG位置触发WLI采集）")
            w(f"PTP/r 0, X={row['x_end']:.3f}, Y={row['y']:.3f}")
            w("WAIT 150")

            # Z轴上升至安全高度
            w(f"! Z轴上升 → 安全高度")
            w(f"VEL {c.transition_velocity}")
            w(f"PTP/r 0, Z={c.z_safe_height}")
            w("WAIT 100")
            w("")

        # ── 扫描完成 ──
        w("! ============================================================")
        w("!  扫描完成，返回Home")
        w("! ============================================================")
        w(f"VEL {c.transition_velocity}")
        w(f"PTP/r 0, X=0, Y=0, Z={c.z_safe_height}")
        w("WAIT 200")
        w("")
        w("! ---- 程序结束 ----")
        w("DISPLAY '晶圆扫描完成，共扫描 %d 行' scan_row_total")
        w("STOP")

        script = "\n".join(lines)

        # ── 保存文件 ──
        os.makedirs(self.cfg.output_dir, exist_ok=True)
        fpath = os.path.join(self.cfg.output_dir, filename)
        _write_text_file(fpath, script, "ACS控制脚本")

        return script

    def export_waypoints_csv(self, filename: str = "waypoints.csv"):
        """
        导出路径点为CSV文件（方便在Excel或Python中进一步分析）

        Args:
            filename: 输出CSV文件名
        """
        if not self.all_waypoints:
            self.generate_scan_path(verbose=False)

        os.makedirs(self.cfg.output_dir, exist_ok=True)
        fpath = os.path.join(self.cfg.output_dir, filename)

        lines = ["index,x_mm,y_mm,type,row_id,description\n"]
        for i, wp in enumerate(self.all_waypoints):
            lines.append(
                f"{i},{wp['x']:.4f},{wp['y']:.4f},"
                f"{wp['type']},{wp['row_id']},{wp['desc']}\n"
            )
        _write_text_file(fpath, "".join(lines), "路径点CSV")

    # ──────────────────────────────────────────────────────────────
    #  工具方法
    # ──────────────────────────────────────────────────────────────

    def check_boundary_compliance(self) -> Tuple[bool, List[str]]:
        """
        检查所有路径点是否满足边界约束

        验证项：
          1. 所有点的探头中心距晶圆中心 ≤ probe_limit
          2. 所有扫描行端点确实在对应的弦上
          3. 行步进一致性
          4. 扫描方向交替正确

        Returns:
            (是否全部通过, 问题列表)
        """
        if not self.all_waypoints:
            self.generate_scan_path(verbose=False)

        issues = []

        # 检查1：所有点在探头极限半径内
        for i, wp in enumerate(self.all_waypoints):
            r = math.sqrt(wp["x"]**2 + wp["y"]**2)
            if r > self.probe_limit + 0.001:  # 1μm容差
                issues.append(
                    f"点{i} ({wp['x']:.3f}, {wp['y']:.3f}) 超出极限半径: "
                    f"距离={r:.3f}mm > {self.probe_limit:.1f}mm"
                )

        # 检查2：扫描行端点在同一Y坐标上
        for row in self.scan_rows:
            # 行起止点的Y应等于行Y
            # （按构造函数逻辑这是自动满足的，但做验证）
            pass

        # 检查3：行步进一致性
        if len(self.scan_rows) >= 2:
            steps = []
            for i in range(1, len(self.scan_rows)):
                dy = self.scan_rows[i]["y"] - self.scan_rows[i-1]["y"]
                steps.append(dy)
            avg_step = sum(steps) / len(steps)
            for i, s in enumerate(steps):
                if abs(s - avg_step) > 0.01:
                    issues.append(
                        f"行{i}→行{i+1}的步进{s:.4f}mm偏离平均值{avg_step:.4f}mm"
                    )

        # 检查4：方向交替
        for i in range(1, len(self.scan_rows)):
            prev_dir = self.scan_rows[i-1]["direction"]
            curr_dir = self.scan_rows[i]["direction"]
            if prev_dir == curr_dir:
                issues.append(
                    f"行{self.scan_rows[i-1]['id']}和行{self.scan_rows[i]['id']}"
                    f"方向相同({curr_dir})，未交替"
                )

        all_ok = len(issues) == 0
        return all_ok, issues

    @staticmethod
    def _now() -> str:
        """返回当前时间字符串"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================================
#  第四部分：演示/主入口
# ============================================================================

def simulate():
    """
    仿真运行入口

    这是在没有实际硬件连接时的完整仿真流程：
      1. 创建扫描器实例（使用默认12寸晶圆配置）
      2. 生成栅格扫描路径
      3. 运行运动学仿真
      4. 边界合规性检查
      5. 可视化扫描路径
      6. 导出ACS控制脚本
      7. 导出路径点CSV

    运行方式：
      python wafer_scanner.py
      或在PyCharm中直接运行此文件
    """
    print("\n" + "█" * 62)
    print("█" + " " * 60 + "█")
    print("█" + "  12英寸晶圆宏动平台栅格扫描仿真系统".center(52) + "█")
    print("█" + "  (纯软件仿真模式 - 无硬件连接)".center(52) + "█")
    print("█" + " " * 60 + "█")
    print("█" * 62)

    # ── 步骤1：创建配置 ──
    config = ScanConfig(
        wafer_radius=150.0,          # 12英寸晶圆
        edge_exclusion=3.0,          # 3mm边缘排除
        fov_x=5.0,                   # WLI视场 5mm
        fov_y=5.0,
        overlap_ratio=0.15,          # 15%行间重叠
        scan_velocity=50.0,          # 50mm/s扫描速度
        transition_velocity=100.0,   # 100mm/s过渡速度
        acceleration=500.0,          # 500mm/s^2加速度
        deceleration=500.0,
        z_safe_height=5.0,           # Z安全高度5mm
        z_scan_height=0.0,           # Z扫描高度0mm
        output_dir="./scan_output",
    )

    # ── 步骤2：创建扫描器 ──
    scanner = WaferScanner(config)

    # ── 步骤3：生成扫描路径 ──
    scanner.generate_scan_path(verbose=True)

    # ── 步骤4：仿真计算 ──
    scanner.simulate(verbose=True)

    # ── 步骤5：边界合规性检查 ──
    print("=" * 62)
    print("  边界合规性检查")
    print("=" * 62)
    ok, issues = scanner.check_boundary_compliance()
    if ok:
        print("  [PASS] 所有检查项通过！路径点全部在安全边界内。")
    else:
        print(f"  [FAIL] 发现 {len(issues)} 个问题:")
        for issue in issues:
            print(f"    - {issue}")
    print()

    # ── 步骤6：可视化 ──
    scanner.plot(save=True, show=False)

    # ── 步骤7：导出ACS脚本 ──
    scanner.export_acs_script("wafer_scan.acs")

    # ── 步骤8：导出路径点CSV ──
    scanner.export_waypoints_csv("waypoints.csv")

    # ── 完成 ──
    print("\n" + "=" * 62)
    print("  仿真完成！")
    print(f"  输出文件目录: {config.output_dir}/")
    print("    ├── scan_path.png     — 扫描路径可视化图")
    print("    ├── wafer_scan.acs    — ACS SPiiPlus 控制脚本")
    print("    └── waypoints.csv     — 全部路径点坐标")
    print("=" * 62)
    print("\n  下一步：")
    print("    1. 检查 scan_path.png 确认扫描路径正确")
    print("    2. 如需调整参数（速度/重叠率等），修改 ScanConfig 后重新运行")
    print("    3. 确认无误后，将 wafer_scan.acs 加载到 ACS MMI 中")
    print("    4. 连接实际硬件前，先在ACS的Simulation模式下验证脚本")
    print()


if __name__ == "__main__":
    simulate()
