import urllib.request

codes = {
    '02111': '超盈国际控股',
    '00382': '中汇集团',
    '00098': '兴发铝业',
}

for code, name in codes.items():
    url = f'https://qt.gtimg.cn/q=hk{code}'
    resp = urllib.request.urlopen(url, timeout=10)
    raw = resp.read()
    # 尝试不同编码
    line = None
    for enc in ['utf-8', 'gbk', 'gb2312', 'big5']:
        try:
            line = raw.decode(enc)
            break
        except:
            continue
    
    parts = line.split('~')
    
    # 打印关键索引
    print(f'=== {code} (field count={len(parts)}) ===')
    for i in [1, 2, 3, 30, 31, 32, 33, 37, 38, 39, 40, 41, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70]:
        if i < len(parts):
            print(f'  [{i}] = {parts[i]}')
    print()
