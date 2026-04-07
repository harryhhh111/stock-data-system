# Fix FCF calculation logic
# Read the current file
with open('/root/projects/stock_data/fetchers/us_financial.py', 'r') as f:
    lines = f.readlines()

# Find the FCF calculation section
fcf_start = -1
for i, line in enumerate(lines):
    if '# ── 自动计算 free_cash_flow' in line:
        fcf_start = i
        break

if fcf_start == -1:
    print("ERROR: Could not find FCF calculation section")
    exit(1)

# Find the end of this section (next method definition)
fcf_end = -1
for i in range(fcf_start + 1, len(lines)):
    if lines[i].strip().startswith('def '):
        fcf_end = i
        break

if fcf_end == -1:
    fcf_end = len(lines)

# Replace the FCF calculation logic
new_fcf_logic = '''        # ── 自动计算 free_cash_flow（如果 tag_mapping 是 CASHFLOW_TAGS）──
        # 如果 free_cash_flow 为空，但有 net_cash_from_operations 和 capital_expenditures，
        # 则计算 FCF = CFO - CapEx
        # 注意：CapEx 通常是负数（现金流出），但计算 FCF 时应使用绝对值
        if "free_cash_flow" in tag_mapping.values():
            # 确保 free_cash_flow 列存在
            if "free_cash_flow" not in wide.columns:
                wide["free_cash_flow"] = pd.Series(dtype=float)
            
            # 只在 free_cash_flow 为空的行计算
            mask = wide["free_cash_flow"].isna()
            if mask.any():
                cfo = wide.get("net_cash_from_operations")
                capex = wide.get("capital_expenditures")
                
                if cfo is not None and capex is not None:
                    # 计算逻辑：FCF = CFO - CapEx（CapEx 通常是负数，所以实际上是加）
                    # 如果 CapEx 是正数，表示现金流入（出售资产），此时应该用负值
                    # 但根据 SEC 标准，CapEx 通常是负数（现金流出）
                    calculated_fcf = cfo - capex
                    
                    # 只更新之前为空的值
                    wide.loc[mask, "free_cash_flow"] = calculated_fcf[mask]

        return wide

'''

# Build new file
new_lines = lines[:fcf_start] + [new_fcf_logic] + lines[fcf_end:]

# Write back
with open('/root/projects/stock_data/fetchers/us_financial.py', 'w') as f:
    f.writelines(new_lines)

print(f"✓ FCF calculation logic updated (lines {fcf_start}-{fcf_end})")
print(f"✓ File saved successfully")
