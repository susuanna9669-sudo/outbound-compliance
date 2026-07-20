# -*- coding: utf-8 -*-
"""
外呼合规监控工具 V4 - 性能优化版
风控规则：
  R1-外呼上限: 所有号码30天内<=4次
  R2-接通上限: 已接听号码15天内<=1次
  R3-语音助手/A意向/B意向屏蔽
"""
import json, os, io, time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
DB_PATH = os.path.join(os.path.dirname(__file__), '数据库.json')
DB_GZ_PATH = DB_PATH + '.gz'

# 如果gzip存在而json不存在，自动解压
if not os.path.exists(DB_PATH) and os.path.exists(DB_GZ_PATH):
    import gzip
    print('首次启动，解压数据库...')
    with gzip.open(DB_GZ_PATH, 'rb') as f_in:
        with open(DB_PATH, 'wb') as f_out:
            import shutil; shutil.copyfileobj(f_in, f_out)
    print('解压完成')

with open(DB_PATH, 'r', encoding='utf-8') as f:
    DB = json.load(f)
NUMBERS_DB = DB['data']
ANSWERED_SET = {'已接听'}
BLOCKED_STATUS = {'空号', '停机'}
BLOCKED_REGIONS = ['新疆', '西藏']

# 预解析时间戳（只做一次，后续check复用）
print('预解析时间戳中...', end=' ')
t0 = time.time()
for phone, calls in NUMBERS_DB.items():
    for c in calls:
        c['ts'] = datetime.strptime(c['t'], '%Y-%m-%d %H:%M:%S').timestamp()
print(f'完成 ({time.time()-t0:.1f}s)')

ANALYSIS_CACHE = {}

def check_number(phone_str, ref_date):
    calls = NUMBERS_DB.get(phone_str, [])
    if not calls:
        return True, '', {
            'phone': phone_str, 'total_30d': 0, 'total_15d': 0,
            'answered_15d': 0, 'region': '', 'latest_task': '',
            'last_call_time': '', 'days_since_last_call': 999,
            'voice_assistant': False, 'a_intent': False, 'b_intent': False,
            'all_statuses': []
        }

    ref_ts = ref_date.timestamp()
    # 正常自然天窗口：从今天往回数
    ref_day = ref_date.replace(hour=0, minute=0, second=0, microsecond=0)
    d15_ts = (ref_day - timedelta(days=14)).timestamp()  # 含今天共15天
    d30_ts = (ref_day - timedelta(days=29)).timestamp()  # 含今天共30天

    lc = calls[0]
    total_15d = total_30d = a15 = 0
    for c in calls:
        t = c['ts']
        if d30_ts <= t <= ref_ts:
            total_30d += 1
            if d15_ts <= t <= ref_ts:
                total_15d += 1
                if c['s'] in ANSWERED_SET:
                    a15 += 1

    lc = calls[0]
    lt = lc.get('t', '')
    ds = int((ref_ts - lc['ts']) / 86400) if lt else 999
    voice = a_int = b_int = ever_answered = is_empty = is_stopped = False
    for c in calls:
        if c.get('voice'): voice = True
        if c.get('a_intent'): a_int = True
        if c.get('b_intent'): b_int = True
        if c['s'] in ANSWERED_SET: ever_answered = True
        if c['s'] in BLOCKED_STATUS:
            if c['s'] == '空号': is_empty = True
            if c['s'] == '停机': is_stopped = True

    region = lc.get('region', '')
    is_xjxz = any(r in region for r in BLOCKED_REGIONS)

    d = {'phone': phone_str, 'total_30d': total_30d, 'total_15d': total_15d,
         'answered_15d': a15, 'region': region, 'latest_task': lc.get('task',''),
         'last_call_time': lt, 'days_since_last_call': ds,
         'voice_assistant': voice, 'a_intent': a_int, 'b_intent': b_int,
         'ever_answered': ever_answered, 'all_statuses': list(set(c['s'] for c in calls))}

    # 其他屏蔽（按优先级：空号/停机 > 区域 > 语音助手 > A/B意向）
    if is_empty: return False, '号码为空', d
    if is_stopped: return False, '手机停机', d
    if is_xjxz: return False, f'区域限制({region})', d
    if voice: return False, '语音助手', d
    if a_int: return False, 'A意向', d
    if b_int: return False, 'B意向', d

    # 风控拦截（频率规则）
    if total_30d >= 4: return False, f'外呼上限(30天{total_30d}次≥4)', d
    if ever_answered and total_15d > 1: return False, f'接通上限(15天{total_15d}次，该号码历史有已接听记录)', d
    return True, '合规', d

def batch_check_all(ref_date=None):
    if ref_date is None: ref_date = datetime.now()
    cl, fb, ob = [], [], []
    phones = list(NUMBERS_DB.keys())
    for phone in phones:
        ok, reason, d = check_number(phone, ref_date)
        if ok:
            cl.append(d)
        else:
            d['block_reason'] = reason
            (ob if reason in ('语音助手','A意向','B意向','号码为空','手机停机') or reason.startswith('区域限制') else fb).append(d)
    cl.sort(key=lambda x: -x.get('days_since_last_call', 0))
    s = {'total': len(phones), 'compliant': len(cl), 'freq_block': len(fb),
         'other_block': len(ob), 'ref_date': ref_date.strftime('%Y-%m-%d %H:%M')}
    return s, cl, fb, ob

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/stats')
def get_stats():
    return jsonify({'total_phones': len(NUMBERS_DB), 'total_calls': sum(len(v) for v in NUMBERS_DB.values()),
                    'ref_date': DB.get('ref_date','2026-07-07')})

@app.route('/api/analyze', methods=['POST'])
def analyze():
    """分析所有号码，仅返回预览数据"""
    import traceback
    try:
        ref_date = datetime.now()
        s, cl, fb, ob = batch_check_all(ref_date)
        ANALYSIS_CACHE.update({'summary': s, 'compliant': cl, 'freq_block': fb, 'other_block': ob,
                               'ts': datetime.now().isoformat()})
        PL = 300
        return jsonify({'summary': s, 'compliant': cl[:PL], 'freq_block': fb[:PL], 'other_block': ob[:PL]})
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/import', methods=['POST'])
def import_records():
    if 'file' not in request.files: return jsonify({'error':'请上传文件'}),400
    file = request.files['file']
    if file.filename == '': return jsonify({'error':'请选择文件'}),400
    fp = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
    file.save(fp)
    try: new_data = parse_uploaded_file_detailed(fp)
    except Exception as e: return jsonify({'error':f'文件解析失败:{str(e)}'}),400
    if not new_data: return jsonify({'error':'未能提取到有效数据'}),400
    meta = new_data.pop('_meta', {})
    phone_count = len(new_data)
    if phone_count == 0:
        return jsonify({'error': f'文件中未找到有效号码（时间列已识别: {"是" if "time" in meta.get("found_cols",[]) else "否"}，跳过{meta.get("skipped_no_time",0)}条无时间记录）'}), 400
    added = nph = 0
    for phone, recs in new_data.items():
        if phone in NUMBERS_DB:
            et = set(c['t'] for c in NUMBERS_DB[phone])
            ni = [r for r in recs if r['t'] not in et]
            if ni:
                for r in ni:
                    try: r['ts'] = datetime.strptime(r['t'],'%Y-%m-%d %H:%M:%S').timestamp()
                    except: r['ts'] = datetime.now().timestamp()
                NUMBERS_DB[phone].extend(ni)
                NUMBERS_DB[phone].sort(key=lambda x: x['t'], reverse=True)
                added += len(ni)
        else:
            for r in recs:
                try: r['ts'] = datetime.strptime(r['t'],'%Y-%m-%d %H:%M:%S').timestamp()
                except: r['ts'] = datetime.now().timestamp()
            recs.sort(key=lambda x: x['t'], reverse=True)
            NUMBERS_DB[phone] = recs; added += len(recs); nph += 1
    DB['data'] = NUMBERS_DB; DB['ref_date'] = datetime.now().strftime('%Y-%m-%d')
    with open(DB_PATH,'w',encoding='utf-8') as f: json.dump(DB,f,ensure_ascii=False,separators=(',',':'))
    return jsonify({'success':True,'new_phones':nph,'new_records':added,'total_phones':len(NUMBERS_DB)})

def parse_uploaded_file_detailed(filepath):
    import pandas as pd
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.csv':
        try: df = pd.read_csv(filepath, dtype=str, encoding='utf-8')
        except: df = pd.read_csv(filepath, dtype=str, encoding='gbk')
    elif ext in ('.xlsx','.xls'): df = pd.read_excel(filepath, dtype=str)
    else: raise ValueError(f'不支持: {ext}')
    cm = {}
    for col in df.columns:
        cl = str(col).lower()
        # 手机号：排除"号码归属地"这类干扰列
        is_phone = any(k in cl for k in ['联系电话','手机号','手机号码','phone','mobile'])
        is_phone = is_phone or (any(k in cl for k in ['电话','手机']) and '归属' not in cl and '地区' not in cl)
        is_phone = is_phone or (any(k in cl for k in ['号码','tel','联系']) and '归属' not in cl and '地区' not in cl)
        if is_phone and 'phone' not in cm: cm['phone']=col
        elif any(k in cl for k in ['时间','日期','time','date','呼叫']) and 'time' not in cm: cm['time']=col
        elif any(k in cl for k in ['任务','task','项目','活动']) and 'task' not in cm: cm['task']=col
        elif any(k in cl for k in ['状态','结果','status','接听']) and 'status' not in cm: cm['status']=col
        elif any(k in cl for k in ['归属','地区','region','城市','省份']) and 'region' not in cm: cm['region']=col
        elif any(k in cl for k in ['意向','分类','intent','标签']) and 'intent' not in cm: cm['intent']=col
    if 'phone' not in cm:
        for col in df.columns:
            if df[col].dropna().apply(lambda x: sum(c.isdigit() for c in str(x))>7).mean() > 0.7:
                cm['phone']=col; break
    if 'phone' not in cm: cm['phone']=df.columns[0]

    # 检查是否有时间和手机号列
    missing = []
    if 'phone' not in cm: missing.append('手机号')
    if 'time' not in cm: missing.append('呼叫时间')
    if missing:
        found_cols = ', '.join(str(c) for c in df.columns[:8])
        raise ValueError(f'未识别到{",".join(missing)}列。当前文件列: {found_cols}...。请确保文件包含手机号和呼叫时间列')

    # 检查手机号列是否真的有手机号
    sample = df[cm['phone']].dropna().head(5).tolist()
    digit_counts = [sum(c.isdigit() for c in str(v)) for v in sample]
    if max(digit_counts) < 7:
        raise ValueError(f'手机号列"{cm["phone"]}"未识别到有效手机号(样例:{sample})')

    vp = set()
    if 'intent' in cm:
        for _,r in df.iterrows():
            if '语音' in str(r.get(cm['intent'],'')) or '助手' in str(r.get(cm['intent'],'')):
                p = ''.join(filter(str.isdigit,str(r.get(cm['phone'],''))))
                if len(p)>=7: vp.add(p)
    res = {}
    skip_no_time = 0
    for _,r in df.iterrows():
        p = ''.join(filter(str.isdigit,str(r.get(cm['phone'],''))))
        if len(p)<7: continue
        raw_t = str(r[cm['time']]).strip() if pd.notna(r.get(cm['time'])) else ''
        if not raw_t:
            skip_no_time += 1
            continue
        rec = {'t': raw_t, 's':'','task':'','region':'','voice':p in vp,'a_intent':False,'b_intent':False}
        if 'status' in cm and pd.notna(r.get(cm['status'])): rec['s']=str(r[cm['status']]).strip()
        if 'task' in cm and pd.notna(r.get(cm['task'])): rec['task']=str(r[cm['task']]).strip()
        if 'region' in cm and pd.notna(r.get(cm['region'])): rec['region']=str(r[cm['region']]).strip()
        res.setdefault(p,[]).append(rec)
    res['_meta'] = {'found_cols': list(cm.keys()), 'skipped_no_time': skip_no_time, 'total_valid': sum(len(v) for v in res.values())}
    return res

def fast_excel(items, title, headers, col_keys, hc='1a1a2e'):
    """基于pandas+openpyxl的快速Excel导出"""
    import pandas as pd
    rows = []
    for i, item in enumerate(items, 1):
        r = [i]  # 序号
        for k in col_keys:
            v = item.get(k,'')
            if isinstance(v,list): v = '、'.join(str(x) for x in v[:20])
            elif isinstance(v,bool): v = '是' if v else '否'
            r.append(v)
        rows.append(r)
    df = pd.DataFrame(rows, columns=headers)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as w:
        df.to_excel(w, sheet_name=title, index=False)
    out.seek(0)
    return out

@app.route('/api/download/compliant', methods=['POST'])
def download_compliant():
    items = ANALYSIS_CACHE.get('compliant', [])
    if not items: return jsonify({'error':'请先分析'}),400
    h = ['序号','手机号','话术分类','最新外呼任务','归属地','最后呼叫时间','距今天数','30天呼叫','15天呼叫','15天接听','建议优先']
    k = ['phone','script_type','latest_task','region','last_call_time','days_since_last_call','total_30d','total_15d','answered_15d','priority']
    for item in items:
        d = item.get('days_since_last_call',999)
        item['priority'] = '高(>14天)' if d>=14 else ('中(7-14天)' if d>=7 else '常规')
        item['script_type'] = '已接听话术' if item.get('ever_answered') else '非已接听话术'
    out = fast_excel(items, '合规外呼名单', h, k)
    return send_file(out, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=f'合规外呼名单_{datetime.now().strftime("%Y%m%d")}.xlsx')

@app.route('/api/download/freq_block', methods=['POST'])
def download_freq_block():
    items = ANALYSIS_CACHE.get('freq_block', [])
    if not items: return jsonify({'error':'请先分析'}),400
    h = ['序号','手机号','最新任务','归属地','拦截原因','30天呼叫','15天呼叫','15天接听','最后呼叫时间']
    k = ['phone','latest_task','region','block_reason','total_30d','total_15d','answered_15d','last_call_time']
    out = fast_excel(items, '风控拦截名单', h, k, 'c0392b')
    return send_file(out, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=f'风控拦截名单_{datetime.now().strftime("%Y%m%d")}.xlsx')

@app.route('/api/download/other_block', methods=['POST'])
def download_other_block():
    items = ANALYSIS_CACHE.get('other_block', [])
    if not items: return jsonify({'error':'请先分析'}),400
    h = ['序号','手机号','最新任务','归属地','屏蔽原因','历史状态','最后呼叫时间']
    k = ['phone','latest_task','region','block_reason','all_statuses','last_call_time']
    out = fast_excel(items, '其他屏蔽名单', h, k, '7f8c8d')
    return send_file(out, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=f'其他屏蔽名单_{datetime.now().strftime("%Y%m%d")}.xlsx')

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    import sys; sys.stdout.reconfigure(encoding='utf-8')
    print(f'=== 外呼合规监控工具 V4 启动 ===')
    print(f'数据库: {len(NUMBERS_DB)} 个号码, {sum(len(v) for v in NUMBERS_DB.values())} 条记录')
    print(f'访问: http://127.0.0.1:5000')
    try:
        from waitress import serve
        print(f'模式: 生产模式(waitress) - 更稳定')
        serve(app, host='127.0.0.1', port=5000, threads=4)
    except ImportError:
        print(f'模式: 开发模式(flask)')
        app.run(debug=False, host='127.0.0.1', port=5000)
