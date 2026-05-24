cat > ping.py << 'EOF'
from flask import Flask, request, jsonify
import requests
import sys
import datetime
import threading
import logging
import os
import re
import time
import subprocess
import queue
import csv
from io import StringIO

# ========== Termux 后台防休眠 ==========
try:
    subprocess.run(["termux-wake-lock"], check=False)
    print("✅ 后台锁已启动：关屏幕继续运行")
except:
    pass

# -------------------------- 基础配置 完全不动 --------------------------
COUNT_FILE = "report_counter.txt"
ADMIN_GROUP = "APT 🚘 TEST"
LOCKED_MODE = False

GOOGLE_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbzyodzu2iw7oh4i20QbYXZ7goZ4M3UU_uohqpLNFKSVLJMGUSnJ6wU-cfRdFYVQSNPleA/exec"
DYNAMIC_INTERVAL_URL = "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1044256272"
DEFAULT_RELOAD_MINUTES = 10

# -------------------------- 屏蔽多余日志 不动 --------------------------
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
sys.stdout.reconfigure(line_buffering=True)

cli = sys.modules['flask.cli']
cli.show_server_banner = lambda *x: None

app = Flask(__name__)

# ==================== 消息队列（防漏 + 极速） ====================
msg_queue = queue.Queue()
current_response = None
response_lock = threading.Lock()
reload_lock = threading.Lock()

REPLY_DELAY = 0

AUTO_RELOAD_EVENT = threading.Event()
AUTO_RELOAD_STOP_FLAG = False
AUTO_RELOAD_DISABLED_PERMANENT = False
IS_RECAP_RELOAD = False

exclude1 = []

# ==================== 正则缓存优化（纯提速，不改变任何行为） ====================
_regex_cache = {}

def _get_cached_regex(pattern, flags=0):
    key = (pattern, flags)
    if key not in _regex_cache:
        _regex_cache[key] = re.compile(pattern, flags)
    return _regex_cache[key]

# ==================== 【核心终极修复：严格100%对齐规则】
# 地方向 / 自由1/2/3 规则：前后 允许【字母、中文、空格、换行】，禁止【数字、标点、特殊字符】
# 彻底修正原正则写反的致命错误 ====================
def dir_keyword_strict_match(msg_raw, kw):
    pattern = rf'(?<![^\sa-zA-Z\u4e00-\u9fff]){re.escape(kw)}(?![^\sa-zA-Z\u4e00-\u9fff])'
    regex = _get_cached_regex(pattern, re.IGNORECASE)
    return regex.finditer(msg_raw)

# ==================== 【自由1/2/3 专用严格匹配：同地方向完全一致规则】 ====================
def free_keyword_strict_match(msg_raw, kw):
    pattern = rf'(?<![^\sa-zA-Z\u4e00-\u9fff]){re.escape(kw)}(?![^\sa-zA-Z\u4e00-\u9fff])'
    regex = _get_cached_regex(pattern, re.IGNORECASE)
    return regex.search(msg_raw) is not None

# ==================== 【短程地关键词规则：市短地/飞短地专用 严格对齐规则】
# 允许：空格、换行、7个特殊符号(- ~ > : ➡️ ↗️ ↘️)
# 禁止：字母、中文、数字、标点、其他符号 ====================
def short_place_keyword_valid(msg_raw, kw):
    allowed = r'\s\-~>:➡️↗️↘️'
    pattern = rf'(?<![^{allowed}]){re.escape(kw)}(?![^{allowed}])'
    regex = _get_cached_regex(pattern, re.IGNORECASE)
    return regex.search(msg_raw) is not None

# ==================== 【短程价/时关键词规则】前后不能有数字 ====================
def short_price_time_valid(msg_clean, kw):
    pattern = rf'(?<!\d){re.escape(kw)}(?!\d)'
    regex = _get_cached_regex(pattern, re.IGNORECASE)
    return regex.search(msg_clean) is not None

# ==================== 【地关键词】左边界无限制，右边界禁止字母+中文 ====================
def keyword_place_valid(msg_clean, kw):
    pattern = rf'{re.escape(kw)}(?![a-zA-Z\u4e00-\u9fff])'
    regex = _get_cached_regex(pattern, re.IGNORECASE)
    return regex.search(msg_clean) is not None

# ==================== 时间关键词规则 前后不能有数字 ====================
def time_keyword_valid(msg_clean, kw):
    pattern = rf'(?<!\d){re.escape(kw)}(?!\d)'
    regex = _get_cached_regex(pattern, re.IGNORECASE)
    return regex.search(msg_clean) is not None

# -------------------------- 价格专用匹配 --------------------------
def is_price_valid_match(msg_clean, kw):
    pattern = rf'(?<!\d){re.escape(kw)}(?!\d)'
    regex = _get_cached_regex(pattern)
    return regex.search(msg_clean) is not None

# ==================== 格式化输出函数 [无空格/无引号] ====================
def fmt(arr):
    if not arr:
        return "[ ]"
    return "[" + ",".join(arr) + "]"

# ==================== 【最终完美修复】为地方关键字添加 ↗️ 符号（校验、搜索用完全同一个正则，彻底杜绝KLIA误判重复） ====================
def format_place_with_arrow(msg_raw, place_list, dir_positions, valid_func):
    formatted_places = []
    if not place_list or not dir_positions:
        return place_list
    min_dir_pos = min(dir_positions)
    msg_clean = msg_raw.lower()
    
    for place in place_list:
        if not valid_func(msg_clean, place):
            formatted_places.append(place)
            continue
        if valid_func is keyword_place_valid:
            pat = _get_cached_regex(rf'{re.escape(place)}(?![a-zA-Z\u4e00-\u9fff])', re.IGNORECASE)
        else:
            pat = _get_cached_regex(re.escape(place), re.IGNORECASE)
            
        for m in pat.finditer(msg_raw):
            place_pos = m.start()
            if place_pos < min_dir_pos:
                formatted_places.append(f"{place}↗️")
            else:
                formatted_places.append(f"↗️{place}")
    return list(set(formatted_places))

# ==============================================
# 位置查找
# ==============================================
def check_dir_before_place(msg_raw, dir_list, place_list):
    text_raw = msg_raw
    text_lower = msg_raw.lower()

    dir_positions = []
    for d in dir_list:
        for m in dir_keyword_strict_match(text_raw, d):
            dir_positions.append(m.start())

    place_positions = []
    for p in place_list:
        if keyword_place_valid(text_lower, p):
            pattern = _get_cached_regex(rf'{re.escape(p)}(?![a-zA-Z\u4e00-\u9fff])', re.IGNORECASE)
            for m in pattern.finditer(text_raw):
                place_positions.append(m.start())

    if not dir_positions or not place_positions:
        return False

    return min(dir_positions) < min(place_positions)

# ==============================================
# 短程单专用：检查方向在两个地关键词之间
# ==============================================
def check_dir_between_two_places(msg_raw, dir_list, place_list):
    dir_positions = []
    for d in dir_list:
        for m in dir_keyword_strict_match(msg_raw, d):
            dir_positions.append(m.start())
    
    place_positions = []
    for p in place_list:
        if short_place_keyword_valid(msg_raw, p):
            pattern = _get_cached_regex(rf'(?<![^\s]){re.escape(p)}(?![^\s])', re.IGNORECASE)
            for m in pattern.finditer(msg_raw):
                place_positions.append(m.start())
    
    place_positions = sorted(list(set(place_positions)))
    if len(place_positions) < 2 or not dir_positions:
        return False
    
    first_place = min(place_positions)
    second_place = max(place_positions)
    for d_pos in dir_positions:
        if first_place < d_pos < second_place:
            return True
    return False

# ==================== 【XUPDATE专用：强制清空所有关键词、归零重读】 ====================
def clear_all_global_data():
    global d_kl,f_kl,d_klia,f_klia,d_gh,f_gh,u_tour,f_tour,cartype,dir_words
    global free_group1,free_group2,free_group3,time_kl,time_klia,time_gh,time_tour
    global exclude1,exclude,shi_short_d,shi_short_f,shi_short_t,fei_short_d,fei_short_f,fei_short_t
    d_kl = []; f_kl = []
    d_klia = []; f_klia = []
    d_gh = []; f_gh = []
    u_tour = []; f_tour = []
    cartype = []; dir_words = []
    free_group1 = []; free_group2 = []; free_group3 = []
    time_kl = []; time_klia = []; time_gh = []; time_tour = []
    exclude1 = []; exclude = []
    shi_short_d = []; shi_short_f = []; shi_short_t = []
    fei_short_d = []; fei_short_f = []; fei_short_t = []
    global AUTO_RELOAD_DISABLED_PERMANENT, AUTO_RELOAD_STOP_FLAG
    AUTO_RELOAD_DISABLED_PERMANENT = True
    AUTO_RELOAD_STOP_FLAG = True
    AUTO_RELOAD_EVENT.set()

# ==================== 【REUPDATE独立加载函数】 ====================
def reupdate_load_only():
    global d_kl,f_kl,d_klia,f_klia,d_gh,f_gh,u_tour,f_tour,cartype,exclude,dir_words
    global free_group1,free_group2,free_group3,time_kl,time_klia,time_gh,time_tour
    global AUTO_RELOAD_MINUTES,CUSTOM_REPLY_TEXT,WORK_DESCRIPTION,REPLY_DELAY,exclude1
    global shi_short_d,shi_short_f,shi_short_t,fei_short_d,fei_short_f,fei_short_t
    
    buf = []
    def p(line):
        nonlocal buf
        buf.append(line)
        print(line)
    
    AUTO_RELOAD_MINUTES, _ = get_reload_minutes()
    delay_val = read_cell_api("F6").strip()
    REPLY_DELAY = int(delay_val) if delay_val.isdigit() else 0
    CUSTOM_REPLY_TEXT = read_cell_api("F4")
    if not CUSTOM_REPLY_TEXT:
        CUSTOM_REPLY_TEXT = "ON Starex 2020 🙋🏻‍♂️"
    WORK_DESCRIPTION = read_cell_api("B7")

    # ========== 严格按你给的排版顺序读取输出 ==========
    dir_words, l12 = get_keywords("地方向", TABLES[10][1]); buf.append(l12)
    p("")

    d_kl, l1 = get_keywords("首都地", TABLES[0][1]); buf.append(l1)
    d_klia, l3 = get_keywords("机场地", TABLES[2][1]); buf.append(l3)
    d_gh, l5 = get_keywords("云顶地", TABLES[4][1]); buf.append(l5)
    p("")

    cartype, l9 = get_keywords("车型号", TABLES[8][1]); buf.append(l9)
    u_tour, l7 = get_keywords("包车计", TABLES[6][1]); buf.append(l7)
    p("")

    f_kl, l2 = get_keywords("首都价", TABLES[1][1]); buf.append(l2)
    f_klia, l4 = get_keywords("机场价", TABLES[3][1]); buf.append(l4)
    f_gh, l6 = get_keywords("云顶价", TABLES[5][1]); buf.append(l6)
    f_tour, l8 = get_keywords("包车价", TABLES[7][1]); buf.append(l8)
    p("")

    time_kl, l16 = get_keywords("首都时", TABLES[14][1]); buf.append(l16)
    time_klia, l17 = get_keywords("机场时", TABLES[15][1]); buf.append(l17)
    time_gh, l18 = get_keywords("云顶时", TABLES[16][1]); buf.append(l18)
    time_tour, l19 = get关键词("包车时", TABLES[17][1]); buf.append(l19)
    p("")

    shi_short_d, l20 = get_keywords("市短地", TABLES[18][1]); buf.append(l20)
    shi_short_f, l21 = get_keywords("市短价", TABLES[19][1]); buf.append(l21)
    shi_short_t, l22 = get_keywords("市短时", TABLES[20][1]); buf.append(l22)
    p("")

    fei_short_d, l23 = get_keywords("飞短地", TABLES[21][1]); buf.append(l23)
    fei_short_f, l24 = get_keywords("飞短价", TABLES[22][1]); buf.append(l24)
    fei_short_t, l25 = get_keywords("飞短时", TABLES[23][1]); buf.append(l25)
    p("")

    free_group1, l13 = get_keywords("自由１", TABLES[11][1]); buf.append(l13)
    free_group2, l14 = get_keywords("自由２", TABLES[12][1]); buf.append(l14)
    free_group3, l15 = get_keywords("自由３", TABLES[13][1]); buf.append(l15)
    p("")

    exclude1, l10 = get_keywords("排除词", TABLES[9][1]); buf.append(l10)
    exclude = exclude1.copy()

# ==================== 处理逻辑 ====================
def process_message(data):
    global report_count, LOCKED_MODE, REPLY_DELAY
    global AUTO_RELOAD_DISABLED_PERMANENT, AUTO_RELOAD_STOP_FLAG, AUTO_RELOAD_EVENT
    global exclude1, IS_RECAP_RELOAD
    global shi_short_d,shi_short_f,shi_short_t,fei_short_d,fei_short_f,fei_short_t
    msg_raw = str(data.get("message", ""))
    msg_clean = msg_raw.strip().lower()
    group = str(data.get("group_name", "")).strip()

    phone = ""
    for k in ["phone","number","sender_phone","from","chat_id"]:
        val = str(data.get(k,"")).strip()
        if val and val!="None" and len(val)>=7:
            phone = val
            break
    if not phone:
        phone = "[无电话]"

    contact_name = ""
    for k in ["contact_name","saved_name"]:
        val = str(data.get(k,"")).strip()
        if val and val!="None" and val!=phone:
            contact_name = val
            break

    wa_nick = ""
    for k in ["sender_name","sender","name"]:
        val = str(data.get(k,"")).strip()
        if val and val!="None" and val!=phone and val!=contact_name:
            wa_nick = val
            break

    parts = []
    if contact_name:
        parts.append(contact_name)
    if wa_nick:
        parts.append(wa_nick)
    parts.append(phone)
    sender_show = " | ".join(parts)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if "///" in msg_raw:
        return {"reply": ""}

    current_num = report_count
    report_count += 1
    save_count(report_count)

    if LOCKED_MODE:
        if msg_clean == "resume":
            log_lines = []
            log_lines.append(f"\n🔽🔽🔽 信息拆解 #{current_num:04d}")
            log_lines.append(f"⏱️ {now}")
            log_lines.append(f"👥 群聊: {group}")
            log_lines.append(f"👤 发帖人: {sender_show}")
            log_lines.append(f"📝 信息原文: {msg_raw}")
            log_lines.append("")
            log_lines.append("匹配结果：")
            log_lines.append("⚪ 收到 RESUME 特别解锁指令")
            log_lines.append("")
            log_lines.append("✅ 已解锁，程序恢复正常运作")
            log_lines.append("===========================")
            log_str = "\n".join(log_lines)
            print(log_str)
            send_log_to_gs(log_str)
            LOCKED_MODE = False
            return {"reply": log_str, "admin_reply": log_str}
        else:
            return {"reply": ""}

    has_r_cmd = "llrll" in msg_raw.lower()

    if group == ADMIN_GROUP and "xupdate" in msg_clean:
        clear_all_global_data()
        log_msg = "✅ 指令 XUPDATE 已执行\n🔴 重读系统已彻底关闭\n⏱️ 重读倒计时已归零\n🗑️ 所有旧关键词、旧重读信息已永久删除\n📶 程序停止自动刷新"
        print(log_msg)
        send_log_to_gs(log_msg)
        return {"reply": log_msg, "admin_reply": log_msg}

    if group == ADMIN_GROUP and "reupdate" in msg_clean:
        AUTO_RELOAD_DISABLED_PERMANENT = False
        AUTO_RELOAD_STOP_FLAG = False
        AUTO_RELOAD_EVENT.set()
        reupdate_load_only()
        
        mins = AUTO_RELOAD_MINUTES
        wait_sec = mins * 60
        next_time = datetime.datetime.fromtimestamp(time.time()+wait_sec).strftime('%H:%M:%S')
        next_line = f"⏰ 自动重读：每 {mins} 分钟 | 下次：{next_time}\n==========================="

        recap_buf = []
        recap_buf.append("♻️♻️ RECAP - 程序读取最新操作信息")
        recap_buf.append("♻️♻️ 重读系统已重新开启")
        recap_buf.append(f"✅ 重读间隔：{AUTO_RELOAD_MINUTES} 分钟")
        recap_buf.append(f"✅ 回复延迟：{REPLY_DELAY} 秒")
        recap_buf.append("📌 工作简述：")
        recap_buf.append(WORK_DESCRIPTION if WORK_DESCRIPTION else "（空）")
        recap_buf.append("")
        recap_buf.append(f"✅ [地方向] 成功读取：{len(dir_words)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [首都地] 成功读取：{len(d_kl)}")
        recap_buf.append(f"✅ [机场地] 成功读取：{len(d_klia)}")
        recap_buf.append(f"✅ [云顶地] 成功读取：{len(d_gh)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [车型号] 成功读取：{len(cartype)}")
        recap_buf.append(f"✅ [包车计] 成功读取：{len(u_tour)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [首都价] 成功读取：{len(f_kl)}")
        recap_buf.append(f"✅ [机场价] 成功读取：{len(f_klia)}")
        recap_buf.append(f"✅ [云顶价] 成功读取：{len(f_gh)}")
        recap_buf.append(f"✅ [包车价] 成功读取：{len(f_tour)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [首都时] 成功读取：{len(time_kl)}")
        recap_buf.append(f"✅ [机场时] 成功读取：{len(time_klia)}")
        recap_buf.append(f"✅ [云顶时] 成功读取：{len(time_gh)}")
        recap_buf.append(f"✅ [包车时] 成功读取：{len(time_tour)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [市短地] 成功读取：{len(shi_short_d)}")
        recap_buf.append(f"✅ [市短价] 成功读取：{len(shi_short_f)}")
        recap_buf.append(f"✅ [市短时] 成功读取：{len(shi_short_t)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [飞短地] 成功读取：{len(fei_short_d)}")
        recap_buf.append(f"✅ [飞短价] 成功读取：{len(fei_short_f)}")
        recap_buf.append(f"✅ [飞短时] 成功读取：{len(fei_short_t)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [自由１] 成功读取：{len(free_group1)}")
        recap_buf.append(f"✅ [自由２] 成功读取：{len(free_group2)}")
        recap_buf.append(f"✅ [自由３] 成功读取：{len(free_group3)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [排除词] 成功读取：{len(exclude1)}")
        recap_buf.append("")
        recap_buf.append(next_line)

        recap_str = "\n".join(recap_buf)
        print(recap_str)
        send_log_to_gs(recap_str)
        return {"reply": recap_str, "admin_reply": recap_str}

    if group == ADMIN_GROUP and "recap" in msg_clean:
        clear_all_global_data()
        AUTO_RELOAD_DISABLED_PERMANENT = False
        AUTO_RELOAD_STOP_FLAG = False
        AUTO_RELOAD_EVENT.set()
        IS_RECAP_RELOAD = True
        reupdate_load_only()

        mins = AUTO_RELOAD_MINUTES
        wait_sec = mins * 60
        next_time = datetime.datetime.fromtimestamp(time.time()+wait_sec).strftime('%H:%M:%S')
        next_line = f"⏰ 自动重读：每 {mins} 分钟 | 下次：{next_time}\n==========================="

        recap_buf = []
        recap_buf.append("♻️♻️ RECAP - 程序读取最新操作信息")
        recap_buf.append("")
        recap_buf.append("🔴 第一步：XUPDATE 关闭重读、清空旧数据")
        recap_buf.append("♻️♻️ 第二步：REUPDATE 开启重读、加载新数据")
        recap_buf.append("")
        recap_buf.append(f"✅ 重读间隔：{AUTO_RELOAD_MINUTES} 分钟")
        recap_buf.append(f"✅ 回复延迟：{REPLY_DELAY} 秒")
        recap_buf.append("📌 工作简述：")
        recap_buf.append(WORK_DESCRIPTION if WORK_DESCRIPTION else "（空）")
        recap_buf.append("")
        recap_buf.append(f"✅ [地方向] 成功读取：{len(dir_words)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [首都地] 成功读取：{len(d_kl)}")
        recap_buf.append(f"✅ [机场地] 成功读取：{len(d_klia)}")
        recap_buf.append(f"✅ [云顶地] 成功读取：{len(d_gh)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [车型号] 成功读取：{len(cartype)}")
        recap_buf.append(f"✅ [包车计] 成功读取：{len(u_tour)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [首都价] 成功读取：{len(f_kl)}")
        recap_buf.append(f"✅ [机场价] 成功读取：{len(f_klia)}")
        recap_buf.append(f"✅ [云顶价] 成功读取：{len(f_gh)}")
        recap_buf.append(f"✅ [包车价] 成功读取：{len(f_tour)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [首都时] 成功读取：{len(time_kl)}")
        recap_buf.append(f"✅ [机场时] 成功读取：{len(time_klia)}")
        recap_buf.append(f"✅ [云顶时] 成功读取：{len(time_gh)}")
        recap_buf.append(f"✅ [包车时] 成功读取：{len(time_tour)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [市短地] 成功读取：{len(shi_short_d)}")
        recap_buf.append(f"✅ [市短价] 成功读取：{len(shi_short_f)}")
        recap_buf.append(f"✅ [市短时] 成功读取：{len(shi_short_t)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [飞短地] 成功读取：{len(fei_short_d)}")
        recap_buf.append(f"✅ [飞短价] 成功读取：{len(fei_short_f)}")
        recap_buf.append(f"✅ [飞短时] 成功读取：{len(fei_short_t)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [自由１] 成功读取：{len(free_group1)}")
        recap_buf.append(f"✅ [自由２] 成功读取：{len(free_group2)}")
        recap_buf.append(f"✅ [自由３] 成功读取：{len(free_group3)}")
        recap_buf.append("")
        recap_buf.append(f"✅ [排除词] 成功读取：{len(exclude1)}")
        recap_buf.append("")
        recap_buf.append(next_line)

        recap_str = "\n".join(recap_buf)
        print(recap_str)
        send_log_to_gs(recap_str)
        return {"reply": recap_str, "admin_reply": recap_str}

    hit_exclude = [w for w in exclude if w in msg_clean]
    has_exclude = len(hit_exclude) > 0

    # ==================== 【严格匹配合格地方向词】
    qualified_dir = []
    for w in dir_words:
        for m in dir_keyword_strict_match(msg_raw, w):
            qualified_dir.append((m.start(), w))
    qualified_dir = sorted(qualified_dir, key=lambda x: x[0])
    dir_words_found = [w for (p, w) in qualified_dir]
    dir_show_list = dir_words_found.copy()
    if len(dir_show_list) > 1:
        dir_show_list[0] = "✅" + dir_show_list[0]
    dir_show = fmt(dir_show_list)
    hit_dir = dir_words_found

    dir_all_positions = [p for (p, w) in qualified_dir] if qualified_dir else []

    cap_d_raw = [w for w in d_kl if keyword_place_valid(msg_clean, w)]
    air_d_raw = [w for w in d_klia if keyword_place_valid(msg_clean, w)]
    gh_d_raw = [w for w in d_gh if keyword_place_valid(msg_clean, w)]
    shi_d_raw = [w for w in shi_short_d if short_place_keyword_valid(msg_raw, w)]
    fei_d_raw = [w for w in fei_short_d if short_place_keyword_valid(msg_raw, w)]

    cap_d = format_place_with_arrow(msg_raw, cap_d_raw, dir_all_positions, keyword_place_valid)
    air_d = format_place_with_arrow(msg_raw, air_d_raw, dir_all_positions, keyword_place_valid)
    gh_d = format_place_with_arrow(msg_raw, gh_d_raw, dir_all_positions, keyword_place_valid)
    shi_d = format_place_with_arrow(msg_raw, shi_d_raw, dir_all_positions, short_place_keyword_valid)
    fei_d = format_place_with_arrow(msg_raw, fei_d_raw, dir_all_positions, short_place_keyword_valid)

    cap_f = [w for w in f_kl if w in msg_clean and is_price_valid_match(msg_clean, w)]
    air_f = [w for w in f_klia if w in msg_clean and is_price_valid_match(msg_clean, w)]
    gh_f = [w for w in f_gh if w in msg_clean and is_price_valid_match(msg_clean, w)]

    # ==================== 【半场功能 最终精准修复 核心】 ====================
    tour_d = []
    half_tour_keywords = []
    for kw in u_tour:
        clean_kw = kw.replace('•', '').strip()
        if clean_kw and clean_kw in msg_clean:
            tour_d.append(kw)
            if kw.startswith('•') and kw.endswith('•'):
                half_tour_keywords.append(kw)
    is_half_tour = len(half_tour_keywords) > 0

    tour_f = []
    for price_kw in f_tour:
        clean_price = price_kw.replace('•', '').strip()
        if not (clean_price and clean_price in msg_clean and is_price_valid_match(msg_clean, clean_price)):
            continue
        if price_kw.startswith('•') and price_kw.endswith('•'):
            if is_half_tour:
                tour_f.append(price_kw)
        else:
            tour_f.append(price_kw)

    tour_c = [w for w in cartype if w in msg_clean]

    cap_t = [w for w in time_kl if time_keyword_valid(msg_clean, w)]
    air_t = [w for w in time_klia if time_keyword_valid(msg_clean, w)]
    gh_t = [w for w in time_gh if time_keyword_valid(msg_clean, w)]
    tour_t = [w for w in time_tour if time_keyword_valid(msg_clean, w)]

    free1_hit = [w for w in free_group1 if free_keyword_strict_match(msg_raw, w)]
    free2_hit = [w for w in free_group2 if free_keyword_strict_match(msg_raw, w)]
    free3_hit = [w for w in free_group3 if free_keyword_strict_match(msg_raw, w)]
    free_ok = len(free1_hit) > 0 and len(free2_hit) > 0 and len(free3_hit) > 0

    cap_dir_ok = check_dir_before_place(msg_raw, hit_dir, cap_d_raw)
    air_dir_ok = check_dir_before_place(msg_raw, hit_dir, air_d_raw)
    gh_dir_ok = check_dir_before_place(msg_raw, hit_dir, gh_d_raw)

    cap_line_ok = cap_dir_ok and len(cap_f) > 0 and len(cap_t) > 0
    air_line_ok = air_dir_ok and len(air_f) > 0 and len(air_t) > 0
    gh_line_ok = gh_dir_ok and len(gh_f) > 0 and len(gh_t) > 0
    tour_line_ok = len(tour_d) > 0 and len(tour_f) > 0 and len(tour_c) > 0 and len(tour_t) > 0

    shi_f = [w for w in shi_short_f if short_price_time_valid(msg_clean, w)]
    shi_t = [w for w in shi_short_t if short_price_time_valid(msg_clean, w)]
    
    fei_f = [w for w in fei_short_f if short_price_time_valid(msg_clean, w)]
    fei_t = [w for w in fei_short_t if short_price_time_valid(msg_clean, w)]

    shi_short_ok = check_dir_between_two_places(msg_raw, hit_dir, shi_d_raw) and len(shi_d_raw)>=2 and len(shi_f)>0 and len(shi_t)>0
    fei_short_ok = check_dir_between_two_places(msg_raw, hit_dir, fei_d_raw) and len(fei_d_raw)>=2 and len(fei_f)>0 and len(fei_t)>0

    has_any_matched_group = cap_line_ok or air_line_ok or gh_line_ok or tour_line_ok or free_ok or shi_short_ok or fei_short_ok
    final_ok = has_any_matched_group and not has_exclude

    def build_full_report(is_success, delay_sec=0):
        rep = []
        rep.append(f"\n🔽🔽🔽 信息拆解 #{current_num:04d}")
        rep.append(f"⏱️ {now}")
        rep.append(f"👥 群聊: {group}")
        rep.append(f"👤 发帖人: {sender_show}")
        rep.append(f"📝 信息原文: {msg_raw}")
        rep.append("")
        rep.append("匹配结果：")

        d1 = fmt(cap_d)
        p1 = fmt(cap_f)
        t1 = fmt(cap_t)
        if len(hit_dir) > 0 and len(cap_d) > 0:
            mark1 = "✅" if cap_dir_ok else "⚠️"
            ball1 = "⚪" if cap_line_ok else "⚫️"
            rep.append(f"{ball1} 首都: 向{dir_show} 地{d1}{mark1} 价{p1} 时{t1}")
        else:
            rep.append(f"⚫️ 首都: 向{dir_show} 地{d1} 价{p1} 时{t1}")

        d2 = fmt(air_d)
        p2 = fmt(air_f)
        t2 = fmt(air_t)
        if len(hit_dir) > 0 and len(air_d) > 0:
            mark2 = "✅" if air_dir_ok else "⚠️"
            ball2 = "⚪" if air_line_ok else "⚫️"
            rep.append(f"{ball2} 机场: 向{dir_show} 地{d2}{mark2} 价{p2} 时{t2}")
        else:
            rep.append(f"⚫️ 机场: 向{dir_show} 地{d2} 价{p2} 时{t2}")

        d3 = fmt(gh_d)
        p3 = fmt(gh_f)
        t3 = fmt(gh_t)
        if len(hit_dir) > 0 and len(gh_d) > 0:
            mark3 = "✅" if gh_dir_ok else "⚠️"
            ball3 = "⚪" if gh_line_ok else "⚫️"
            rep.append(f"{ball3} 云顶: 向{dir_show} 地{d3}{mark3} 价{p3} 时{t3}")
        else:
            rep.append(f"⚫️ 云顶: 向{dir_show} 地{d3} 价{p3} 时{t3}")

        h_c = fmt(tour_c)
        h_d = fmt(tour_d)
        h_p = fmt(tour_f)
        h_t = fmt(tour_t)
        ball_tour = "⚪" if tour_line_ok else "⚫️"
        rep.append(f"{ball_tour} 包车: 车{h_c} 计{h_d} 价{h_p} 时{h_t}")

        f1 = fmt(free1_hit)
        f2 = fmt(free2_hit)
        f3 = fmt(free3_hit)
        ball_free = "⚪" if free_ok else "⚫️"
        rep.append(f"{ball_free} 自由: １{f1} ２{f2} ３{f3}")

        d_shi = fmt(shi_d)
        p_shi = fmt(shi_f)
        t_shi = fmt(shi_t)
        ball_shi = "⚪" if shi_short_ok else "⚫️"
        rep.append(f"{ball_shi} 市短: 向{dir_show} 地{d_shi} 价{p_shi} 时{t_shi}")

        d_fei = fmt(fei_d)
        p_fei = fmt(fei_f)
        t_fei = fmt(fei_t)
        ball_fei = "⚪" if fei_short_ok else "⚫️"
        rep.append(f"{ball_fei} 飞短: 向{dir_show} 地{d_fei} 价{p_fei} 时{t_fei}")

        rep.append(f"🚫 排除: {fmt(hit_exclude)}")

        rep.append("")
        if is_success:
            if delay_sec > 0:
                rep.append(f"✅ 匹配成功，系统准备发出回复（等待 {delay_sec} 秒）")
                rep.append("请按 Y=马上回复 / 其他键=取消回复（无需回车）")
            else:
                rep.append("✅ 匹配成功，系统发出回复")
            rep.append("===========================")
        else:
            if has_any_matched_group and has_exclude:
                rep.append("匹配失败 💎[研究]💎")
            else:
                rep.append("匹配失败")
            rep.append("===========================")
        return rep

    if final_ok:
        reply_msg = CUSTOM_REPLY_TEXT
        full_report = build_full_report(is_success=True, delay_sec=REPLY_DELAY)
        full_str = "\n".join(full_report)
        print(full_str)
        send_log_to_gs(full_str)

        if REPLY_DELAY <= 0:
            print("✅ 自动回复已按程序发出")
            send_log_to_gs("✅ 自动回复已按程序发出")
            LOCKED_MODE = True
            print("‼️触动自动回复，程序暂停，等待指令‼️‼️‼️")
            send_log_to_gs("‼️触动自动回复，程序暂停，等待指令‼️‼️‼️")
            return {"reply": reply_msg, "admin_reply": full_str + "\n✅ 自动回复已按程序发出\n‼️触动自动回复，程序暂停，等待指令‼️‼️‼️"}

        try:
            char = input("")
        except:
            char = None
            
        if char is not None:
            if char.upper() == "Y":
                log_str = "收到选择指令 “Y”，延迟已经被取消，马上发出回复"
                print(log_str)
                send_log_to_gs(log_str)
                print("✅ 自动回复已按程序发出")
                send_log_to_gs("✅ 自动回复已按程序发出")
                LOCKED_MODE = True
                print("‼️触动自动回复，程序暂停，等待指令‼️‼️‼️")
                send_log_to_gs("‼️触动自动回复，程序暂停，等待指令‼️‼️‼️")
                return {"reply": reply_msg, "admin_reply": full_str + "\n" + log_str + "\n✅ 自动回复已按程序发出\n‼️触动自动回复，程序暂停，等待指令‼️‼️‼️"}
            else:
                log_str = f"收到选择指令 “{char}”，自动回复已经被取消"
                print(log_str)
                send_log_to_gs(log_str)
                return {"reply": "", "admin_reply": full_str + "\n" + log_str}
        else:
            log_str = "选择指令被忽略，将依原来设定发出回复"
            print(log_str)
            send_log_to_gs(log_str)
            print("✅ 自动回复已按程序发出")
            send_log_to_gs("✅ 自动回复已按程序发出")
            LOCKED_MODE = True
            print("‼️触动自动回复，程序暂停，等待指令‼️‼️‼️")
            send_log_to_gs("‼️触动自动回复，程序暂停，等待指令‼️‼️‼️")
            return {"reply": reply_msg, "admin_reply": full_str + "\n" + log_str + "\n✅ 自动回复已按程序发出\n‼️触动自动回复，程序暂停，等待指令‼️‼️‼️"}

    full_report = build_full_report(is_success=False)
    full_str = "\n".join(full_report)
    print(full_str)
    send_log_to_gs(full_str)

    reply_msg = full_str if has_r_cmd else ""
    return {"reply": reply_msg}

# ==================== 极速队列处理器 ====================
def queue_worker():
    while True:
        try:
            data, event = msg_queue.get(timeout=0.1)
            res = process_message(data)
            with response_lock:
                global current_response
                current_response = res
            event.set()
            msg_queue.task_done()
        except queue.Empty:
            continue

# -------------------------- TABLE 配置 --------------------------
TABLES = [
    ("首都地", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=243582564"),
    ("首都价", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1870073446"),
    ("机场地", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=644424949"),
    ("机场价", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1190254783"),
    ("云顶地", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1025145145"),
    ("云顶价", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1219902484"),
    ("包车计", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1259532354"),
    ("包车价", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=387732588"),
    ("车型号", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1088097295"),
    ("排除词", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=260290423"),
    ("地方向", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=292160849"),
    ("自由１", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=554281895"),
    ("自由２", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1440624983"),
    ("自由３", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1657330096"),
    ("首都时", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1305964836"),
    ("机场时", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=2100300904"),
    ("云顶时", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1344468220"),
    ("包车时", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=41699491"),
    ("市短地", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=87963329"),
    ("市短价", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=759175200"),
    ("市短时", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1498327035"),
    ("飞短地", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1076786633"),
    ("飞短价", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=2071985165"),
    ("飞短时", "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1516688276")
]

# -------------------------- 全局变量 --------------------------
d_kl = []
f_kl = []
d_klia = []
f_klia = []
d_gh = []
f_gh = []
u_tour = []
f_tour = []
cartype = []
exclude = []
dir_words = []
free_group1 = []
free_group2 = []
free_group3 = []
time_kl = []
time_klia = []
time_gh = []
time_tour = []
shi_short_d = []
shi_short_f = []
shi_short_t = []
fei_short_d = []
fei_short_f = []
fei_short_t = []

AUTO_RELOAD_MINUTES = DEFAULT_RELOAD_MINUTES
CUSTOM_REPLY_TEXT = ""
WORK_DESCRIPTION = ""
REPLY_DELAY = 0

# -------------------------- 工具函数 --------------------------
def read_cell_api(cell):
    try:
        url = "https://docs.google.com/spreadsheets/d/1aRf7eyHWGvaTFzWYULry3H1KLPbuCfLDozzgZ6bfuYU/export?format=csv&gid=1044256272"
        resp = requests.get(url, timeout=2)
        resp.encoding = 'utf-8'
        reader = csv.reader(StringIO(resp.text), quoting=csv.QUOTE_MINIMAL)
        rows = list(reader)
        
        cell_map = {
            "B7": (6, 1),
            "F4": (3, 5),
            "F6": (5, 5),
            "J5": (4, 9)
        }
        if cell not in cell_map:
            return ""
        r_idx, c_idx = cell_map[cell]
        if len(rows) > r_idx and len(rows[r_idx]) > c_idx:
            return str(rows[r_idx][c_idx]).strip()
        return ""
    except Exception:
        return ""

def clean_keyword(s):
    return s.strip().lower()

def get_reload_minutes():
    try:
        val = read_cell_api("J5").strip()
        if val.isdigit():
            m = int(val)
            return max(m, 1), f"✅ 刷新间隔：{m} 分钟"
        else:
            return DEFAULT_RELOAD_MINUTES, f"⚠️ J5 不是数字，使用默认 {DEFAULT_RELOAD_MINUTES} 分钟"
    except Exception:
        return DEFAULT_RELOAD_MINUTES, f"❌ 读取 J5 失败，使用默认 {DEFAULT_RELOAD_MINUTES} 分钟"

def get_keywords(name, url):
    try:
        headers = {'User-Agent': 'Mozilla.0 (Windows NT 10, Win64, x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        resp = requests.get(url, headers=headers, timeout=3)
        resp.encoding = 'utf-8'
        keywords = []
        for line in resp.text.strip().splitlines():
            kw = clean_keyword(line.split(',')[0])
            if kw:
                keywords.append(kw)
        line = f"✅ [{name}] 成功读取：{len(keywords)}"
        print(line)
        return keywords, line
    except Exception:
        line = f"❌ [{name}] 读取失败"
        print(line)
        return [], line

# -------------------------- 日志上传 --------------------------
def _send_gs_task(log_content):
    try:
        requests.post(GOOGLE_SCRIPT_URL, json={"log": log_content}, timeout=2)
    except Exception:
        pass

def send_log_to_gs(log_content):
    threading.Thread(target=_send_gs_task, args=(log_content,), daemon=True).start()

# -------------------------- 初始化加载函数 --------------------------
def load_all(is_updatekey=False, is_auto_reload=False):
    global d_kl, f_kl, d_klia, f_klia, d_gh, f_gh, u_tour, f_tour, cartype, exclude, dir_words
    global free_group1, free_group2, free_group3
    global time_kl, time_klia, time_gh, time_tour
    global AUTO_RELOAD_MINUTES
    global CUSTOM_REPLY_TEXT, WORK_DESCRIPTION, REPLY_DELAY
    global exclude1,shi_short_d,shi_short_f,shi_short_t,fei_short_d,fei_short_f,fei_short_t
    
    with reload_lock:
        buf = []
        def p(line):
            nonlocal buf
            buf.append(line)
            print(line)

        if is_auto_reload:
            p("♻️♻️ 自动重读 - 程序读取最新操作信息")
        elif is_updatekey:
            p("♻️♻️ ping.py UPDATEKEY 特别指令 - 程序读取最新操作信息")
        else:
            p("♻️♻️ ping.py 初始化 - 程序读取最新操作信息")

        AUTO_RELOAD_MINUTES, msg_interval = get_reload_minutes()
        p(msg_interval)

        delay_val = read_cell_api("F6").strip()
        REPLY_DELAY = int(delay_val) if delay_val.isdigit() else 0
        p(f"✅ 回复延迟：{REPLY_DELAY} 秒")

        CUSTOM_REPLY_TEXT = read_cell_api("F4")
        if not CUSTOM_REPLY_TEXT:
            CUSTOM_REPLY_TEXT = "ON Starex 2020 🙋🏻‍♂️"

        WORK_DESCRIPTION = read_cell_api("B7")
        p(f"📌 工作简述：\n{WORK_DESCRIPTION if WORK_DESCRIPTION else '（空）'}")

        p("")

        dir_words, l12 = get_keywords("地方向", TABLES[10][1]); buf.append(l12)
        p("")

        d_kl, l1 = get_keywords("首都地", TABLES[0][1]); buf.append(l1)
        d_klia, l3 = get_keywords("机场地", TABLES[2][1]); buf.append(l3)
        d_gh, l5 = get_keywords("云顶地", TABLES[4][1]); buf.append(l5)
        p("")

        cartype, l9 = get_keywords("车型号", TABLES[8][1]); buf.append(l9)
        u_tour, l7 = get_keywords("包车计", TABLES[6][1]); buf.append(l7)
        p("")

        f_kl, l2 = get_keywords("首都价", TABLES[1][1]); buf.append(l2)
        f_klia, l4 = get_keywords("机场价", TABLES[3][1]); buf.append(l4)
        f_gh, l6 = get_keywords("云顶价", TABLES[5][1]); buf.append(l6)
        f_tour, l8 = get_keywords("包车价", TABLES[7][1]); buf.append(l8)
        p("")

        time_kl, l16 = get_keywords("首都时", TABLES[14][1]); buf.append(l16)
        time_klia, l17 = get_keywords("机场时", TABLES[15][1]); buf.append(l17)
        time_gh, l18 = get_keywords("云顶时", TABLES[16][1]); buf.append(l18)
        time_tour, l19 = get_keywords("包车时", TABLES[17][1]); buf.append(l19)
        p("")

        shi_short_d, l20 = get_keywords("市短地", TABLES[18][1]); buf.append(l20)
        shi_short_f, l21 = get_keywords("市短价", TABLES[19][1]); buf.append(l21)
        shi_short_t, l22 = get_keywords("市短时", TABLES[20][1]); buf.append(l22)
        p("")

        fei_short_d, l23 = get_keywords("飞短地", TABLES[21][1]); buf.append(l23)
        fei_short_f, l24 = get_keywords("飞短价", TABLES[22][1]); buf.append(l24)
        fei_short_t, l25 = get_keywords("飞短时", TABLES[23][1]); buf.append(l25)
        p("")

        free_group1, l13 = get_keywords("自由１", TABLES[11][1]); buf.append(l13)
        free_group2, l14 = get_keywords("自由２", TABLES[12][1]); buf.append(l14)
        free_group3, l15 = get_keywords("自由３", TABLES[13][1]); buf.append(l15)
        p("")

        exclude1, l10 = get_keywords("排除词", TABLES[9][1]); buf.append(l10)
        exclude = exclude1.copy()

        full_log = "\n".join(buf)
        send_log_to_gs(full_log)

# -------------------------- 自动重读守护进程 --------------------------
def auto_reload_daemon():
    global AUTO_RELOAD_MINUTES, AUTO_RELOAD_STOP_FLAG, AUTO_RELOAD_DISABLED_PERMANENT, IS_RECAP_RELOAD
    AUTO_RELOAD_DISABLED_PERMANENT = False
    AUTO_RELOAD_STOP_FLAG = False
    
    while not AUTO_RELOAD_STOP_FLAG:
        if AUTO_RELOAD_DISABLED_PERMANENT:
            print("\n🔴 自动重读已永久禁用，守护进程退出")
            break
            
        mins = AUTO_RELOAD_MINUTES
        wait_sec = mins * 60
        next_time = datetime.datetime.fromtimestamp(time.time()+wait_sec).strftime('%H:%M:%S')
        line = f"\n⏰ 自动重读：每 {mins} 分钟 | 下次：{next_time}\n==========================="
        print(line)
        send_log_to_gs(line.strip())

        start_wait = time.time()
        while time.time() - start_wait < wait_sec:
            if AUTO_RELOAD_EVENT.is_set() or AUTO_RELOAD_STOP_FLAG or AUTO_RELOAD_DISABLED_PERMANENT:
                break
            time.sleep(1)
        
        AUTO_RELOAD_EVENT.clear()
        if AUTO_RELOAD_STOP_FLAG or AUTO_RELOAD_DISABLED_PERMANENT:
            break

        reload_time_str = datetime.datetime.now().strftime('%H:%M:%S')
        is_recap_trigger = IS_RECAP_RELOAD
        if is_recap_trigger:
            reload_line = f"\n🔄 执行 RECAP 重读指令 {reload_time_str}\n==========================="
            IS_RECAP_RELOAD = False
        else:
            reload_line = f"\n🔄 执行自动重读 {reload_time_str}\n==========================="
        print(reload_line)
        send_log_to_gs(reload_line.strip())
        if not is_recap_trigger:
            load_all(is_updatekey=False, is_auto_reload=True)

def load_count():
    if os.path.exists(COUNT_FILE):
        with open(COUNT_FILE, "r") as f:
            return int(f.read().strip())
    return 1

def save_count(n):
    with open(COUNT_FILE, "w") as f:
        f.write(str(n))

report_count = load_count()
load_all(is_updatekey=False, is_auto_reload=False)
threading.Thread(target=auto_reload_daemon, daemon=True).start()

# ==================== Webhook 入口 ====================
@app.route('/reply', methods=['GET','POST'])
def whatsauto_webhook():
    json_data = request.get_json(silent=True) or {}
    form_data = request.form.to_dict() or {}
    data = {**form_data, **json_data}

    event = threading.Event()
    msg_queue.put((data, event))
    event.wait()

    with response_lock:
        return jsonify(current_response)

# ==================== 启动 ====================
if __name__ == '__main__':
    threading.Thread(target=queue_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)
EOF
