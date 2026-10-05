#!/usr/bin/env python3
"""
补全 Batch1 缺失表格描述的16篇文档 - 辅助脚本
用法: python trim_manifest_for_missing.py
功能:
  1. 备份完整 manifest
  2. 从完整 manifest 中提取16篇缺失文档，创建修剪版
  3. 输出 Trimmed manifest 路径供后续管道使用
"""
import json
import os
import shutil
from datetime import datetime

BASE = '/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow'
MANIFEST_FILE = os.path.join(BASE, 'announcement_document_processing_local', 'processed_files_local.json')
BACKUP_DIR = os.path.join(BASE, 'manifest_backups')

# 16篇缺失文档的 doc_id（精确匹配 processed_files_local.json 中的 key）
MISSING_KEYS = [
    "508609-508609-华安锦江封闭式商业不动产证券投资基金招募说明书-2026-06-24.pdf",
    "508605-508605-华夏保利发展封闭式商业不动产证券投资基金招募说明书-2026-06-23.pdf",
    "508605-508605-华夏基金管理有限公司关于华夏保利发展封闭式商业不动产证券投资基金基金合同及招募说明书提示性公告-2026-06-23.pdf",
    "508096-中航京能国际能源封闭式基础设施证券投资基金-中航京能国际能源封闭式基础设施证券投资基金更新的招募说明书（2026年第5号）-2026-07-06.pdf",
    "508007-中金山高集团高速公路封闭式基础设施证券投资基金-中金山高集团高速公路封闭式基础设施证券投资基金更新招募说明书（2026年第1次重大事项临时更新）-2026-06-24.pdf",
    "508000-华安张江产业园封闭式基础设施证券投资基金-华安张江产业园封闭式基础设施证券投资基金更新的招募说明书（2026年第1号）-2026-06-26.pdf",
    "508090-中银中外运仓储物流封闭式基础设施证券投资基金-中银中外运仓储物流封闭式基础设施证券投资基金更新招募说明书（2026年第1号）-2026-06-25.pdf",
    "508001-浙商证券沪杭甬杭徽高速封闭式基础设施证券投资基金-浙商证券沪杭甬杭徽高速封闭式基础设施证券投资基金招募说明书（2026年定期更新）-2026-06-05.pdf",
    "180106-广发成都高投产业园封闭式基础设施证券投资基金-广发成都高投产业园封闭式基础设施证券投资基金更新的招募说明书-2026-07-01.pdf",
    "508006-富国首创水务封闭式基础设施证券投资基金-富国首创水务封闭式基础设施证券投资基金招募说明书（更新）（2026年第1号）-2026-06-06.pdf",
    "180302-华夏深国际仓储物流封闭式基础设施证券投资基金-华夏深国际仓储物流封闭式基础设施证券投资基金招募说明书更新-2026-06-24.pdf",
    "508008-国金铁建重庆渝遂高速公路封闭式基础设施证券投资基金-国金铁建重庆渝遂高速公路封闭式基础设施证券投资基金招募说明书（更新）-2026-06-17.pdf",
    "180606-中金中国绿发消费封闭式基础设施证券投资基金-中金中国绿发消费封闭式基础设施证券投资基金招募说明书（更新）-2026-06-25.pdf",
    "180201-平安广州交投广河高速公路封闭式基础设施证券投资基金-平安广州交投广河高速公路封闭式基础设施证券投资基金招募说明书（更新）-2026-06-05.pdf",
    "180801-中航首钢生物质封闭式基础设施证券投资基金-中航首钢生物质封闭式基础设施证券投资基金更新的招募说明书（2026年第1号）-2026-06-06.pdf",
    "508078-中航易商仓储物流封闭式基础设施证券投资基金-中航易商仓储物流封闭式基础设施证券投资基金更新的招募说明书（2026年第1号）-2026-06-18.pdf",
]

def main():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # 1. 备份完整 manifest
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(BACKUP_DIR, f'processed_files_local.full_backup_{ts}.json')
    print(f"[1/4] 备份完整 manifest → {backup_path}")
    shutil.copy2(MANIFEST_FILE, backup_path)
    
    # 2. 加载完整 manifest
    print(f"[2/4] 加载完整 manifest...")
    with open(MANIFEST_FILE, 'r', encoding='utf-8') as f:
        full_manifest = json.load(f)
    
    total_keys = len(full_manifest['files'])
    print(f"  完整 manifest: {total_keys} 篇文档")
    
    # 3. 提取16篇缺失文档
    print(f"[3/4] 提取16篇缺失文档...")
    trimmed_files = {}
    found = 0
    not_found = []
    for key in MISSING_KEYS:
        if key in full_manifest['files']:
            trimmed_files[key] = full_manifest['files'][key]
            found += 1
            doc_info = trimmed_files[key]
            fc = doc_info.get('fund_code', '?')
            title = doc_info.get('announcement_title', doc_info.get('file_name', ''))[:50]
            print(f"  ✅ [{fc}] {title}")
        else:
            not_found.append(key)
            print(f"  ❌ 未找到: {key[:80]}")
    
    # 4. 保存修剪版 manifest
    trimmed_manifest = {
        'files': trimmed_files,
        '_note': f'Trimmed manifest for Batch1 missing 16 docs. Created {ts}. Found {found}/{len(MISSING_KEYS)}.',
        '_restore_from': backup_path
    }
    
    trimmed_path = os.path.join(BASE, 'announcement_document_processing_local',
                                 f'processed_files_local.trimmed_{ts}.json')
    print(f"[4/4] 保存修剪版 manifest ({found}篇) → {trimmed_path}")
    with open(trimmed_path, 'w', encoding='utf-8') as f:
        json.dump(trimmed_manifest, f, ensure_ascii=False, indent=2)
    
    # 5. 激活修剪版 manifest（替换原文件）
    print(f"\n⚡ 激活修剪版 manifest...")
    shutil.copy2(trimmed_path, MANIFEST_FILE)
    print(f"  {MANIFEST_FILE} → 现在仅包含 {found} 篇文档")
    
    if not_found:
        print(f"\n⚠️ 未找到 {len(not_found)} 篇：")
        for nf in not_found:
            print(f"  - {nf[:100]}")
    
    print(f"\n✅ 完成！备份路径: {backup_path}")
    print(f"   修剪版路径: {trimmed_path}")
    print(f"   恢复命令: cp {backup_path} {MANIFEST_FILE}")
    return backup_path, found

if __name__ == '__main__':
    main()
