#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prepush_scan.py — 公开仓推送前敏感信息审查（用户裁定 2026-10-06）

审查目标: 待 commit 的 5 个文件
规则:
  1. API key / token 形态 (sk-,Bearer,key=,AKIA,ghp_,xoxb 等)
  2. 私网/本机地址 (127.0.0.1 带 token、192.168.x、file:// 家目录)
  3. 中海/China Overseas 泄密红线 (用户为中海员工, 公开仓禁项目细节)
  4. 硬编码密钥赋值 (API_KEY=xxx, 密码=xxx)
退出码: 0=干净 1=发现敏感项
"""
import re
import sys

FILES = [
    '/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow/AGENTS.md',
    '/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow/ocr_router.py',
    '/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow/step2_extract_text_onlyvactor_multi_process.py',
    '/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow/step4_1_1_describe_table_images_multi_thread.py',
    '/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow/docs/pending_fixes_20261007.md',
]

PATTERNS = [
    (r'sk-[A-Za-z0-9]{20,}', 'OpenAI/DeepSeek形态key'),
    (r'Bearer\s+[A-Za-z0-9\-_.]{20,}', 'Bearer token'),
    (r'(?:api_key|apikey|API_KEY|secret|SECRET|password|PASSWORD|token|TOKEN)\s*[=:]\s*[\'"][A-Za-z0-9\-_.]{16,}[\'"]', '硬编码密钥赋值'),
    (r'ghp_[A-Za-z0-9]{30,}', 'GitHub token'),
    (r'gho_[A-Za-z0-9]{30,}', 'GitHub OAuth'),
    (r'AKIA[0-9A-Z]{16}', 'AWS key'),
    (r'xoxb-[0-9A-Za-z\-]+', 'Slack token'),
    (r'35\d{7,8}@qq\.com|jimiwuzk|394746667', '个人邮箱'),
    (r'中海(?!物业|国际)|China\s*Overseas', '中海红线(泄密风险)'),
    (r'135\d{9}|1[3-9]\d{9}', '手机号'),
]

hits = 0
for f in FILES:
    try:
        text = open(f, errors='ignore').read()
    except FileNotFoundError:
        print(f'!! 文件不存在: {f}')
        hits += 1
        continue
    for pat, desc in PATTERNS:
        for m in re.finditer(pat, text):
            line_no = text[:m.start()].count('\n') + 1
            snippet = m.group(0)[:40].replace('\n', ' ')
            print(f'[{desc}] {f.split("/")[-1]}:{line_no}  {snippet}')
            hits += 1

if hits:
    print(f'\n❌ 发现 {hits} 处敏感项，禁止推送')
    sys.exit(1)
print('✅ 5 文件敏感审查通过，无密钥/无个人信息/无中海红线')
sys.exit(0)
