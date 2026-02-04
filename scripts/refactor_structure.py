#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目结构重构脚本 - 自动化整理文件结构

功能：
1. 创建新的目录结构
2. 移动文件到对应目录
3. 更新导入路径
4. 生成迁移报告

使用方式：
    python refactor_structure.py --dry-run    # 预览（不实际执行）
    python refactor_structure.py              # 执行重构
    python refactor_structure.py --rollback   # 回滚
"""

import os
import sys
import shutil
import re
from pathlib import Path
from typing import Dict, List, Tuple
import argparse
import json
from datetime import datetime


class ProjectRefactor:
    """项目结构重构器"""
    
    # 文件移动映射
    FILE_MOVES = {
        # 核心模块
        'analyzer.py': 'core/analyzer.py',
        'stock_analyzer.py': 'core/stock_analyzer.py',
        'market_analyzer.py': 'core/market_analyzer.py',
        'storage.py': 'core/storage.py',
        
        # 选股模块
        'stock_screener.py': 'screeners/stock_screener.py',
        'strategy_screener.py': 'screeners/strategy_screener.py',
        'Selector.py': 'screeners/Selector.py',
        'select_stock.py': 'screeners/select_stock.py',
        'SectorShift.py': 'screeners/SectorShift.py',
        'selector_configs.json': 'screeners/configs/selector_configs.json',
        
        # 服务模块
        'notification.py': 'services/notification.py',
        'search_service.py': 'services/search_service.py',
        'scheduler.py': 'services/scheduler.py',
        
        # 脚本工具
        'auto_screen_and_analyze.py': 'scripts/auto_screen_and_analyze.py',
        'verify_integration.py': 'scripts/verify_integration.py',
        'test_integration.py': 'scripts/test_integration.py',
        'run.sh': 'scripts/run.sh',
        
        # 文档
        'INTEGRATION_GUIDE.md': 'docs/INTEGRATION_GUIDE.md',
        'INTEGRATION_COMPLETE.md': 'docs/INTEGRATION_COMPLETE.md',
        'README_INTEGRATION.md': 'docs/README_INTEGRATION.md',
        '整合完成说明.md': 'docs/整合完成说明.md',
        'CHANGELOG.md': 'docs/CHANGELOG.md',
        'CONTRIBUTING.md': 'docs/CONTRIBUTING.md',
        'DEPLOY.md': 'docs/DEPLOY.md',
        
        # 测试
        'test_env.py': 'tests/test_env.py',
    }
    
    # 导入路径替换规则
    IMPORT_REPLACEMENTS = {
        'from analyzer import': 'from core.analyzer import',
        'from stock_analyzer import': 'from core.stock_analyzer import',
        'from market_analyzer import': 'from core.market_analyzer import',
        'from storage import': 'from core.storage import',
        
        'from stock_screener import': 'from screeners.stock_screener import',
        'from strategy_screener import': 'from screeners.strategy_screener import',
        'from Selector import': 'from screeners.Selector import',
        'from select_stock import': 'from screeners.select_stock import',
        
        'from notification import': 'from services.notification import',
        'from search_service import': 'from services.search_service import',
        'from scheduler import': 'from services.scheduler import',
        
        'import analyzer': 'import core.analyzer as analyzer',
        'import stock_analyzer': 'import core.stock_analyzer as stock_analyzer',
        'import notification': 'import services.notification as notification',
    }
    
    def __init__(self, project_root: Path, dry_run: bool = False):
        """
        初始化重构器
        
        Args:
            project_root: 项目根目录
            dry_run: 是否为预览模式（不实际执行）
        """
        self.project_root = project_root
        self.dry_run = dry_run
        self.backup_dir = project_root / f'.backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        self.migration_log = []
        
    def create_directories(self):
        """创建新的目录结构"""
        dirs = [
            'core',
            'screeners',
            'screeners/configs',
            'services',
            'scripts',
            'docs',
            'tests',
        ]
        
        print("📁 创建目录结构...")
        for dir_path in dirs:
            full_path = self.project_root / dir_path
            if not self.dry_run:
                full_path.mkdir(parents=True, exist_ok=True)
                # 创建 __init__.py
                if not dir_path.endswith('configs') and not dir_path.endswith('docs'):
                    init_file = full_path / '__init__.py'
                    if not init_file.exists():
                        init_file.touch()
            print(f"  ✓ {dir_path}")
            self.migration_log.append(f"创建目录: {dir_path}")
    
    def backup_project(self):
        """备份项目"""
        if self.dry_run:
            print(f"📦 [预览] 将创建备份: {self.backup_dir}")
            return
        
        print(f"📦 创建备份: {self.backup_dir}")
        
        # 只备份关键文件
        backup_files = ['*.py', '*.md', '*.json', '*.sh', '.env']
        self.backup_dir.mkdir(exist_ok=True)
        
        for pattern in backup_files:
            for file in self.project_root.glob(pattern):
                if file.is_file() and not file.name.startswith('.backup'):
                    shutil.copy2(file, self.backup_dir / file.name)
        
        print(f"  ✓ 备份完成")
        self.migration_log.append(f"创建备份: {self.backup_dir}")
    
    def move_files(self):
        """移动文件到新位置"""
        print("\n📦 移动文件...")
        
        for src, dst in self.FILE_MOVES.items():
            src_path = self.project_root / src
            dst_path = self.project_root / dst
            
            if not src_path.exists():
                print(f"  ⚠️  跳过（不存在）: {src}")
                continue
            
            if self.dry_run:
                print(f"  [预览] {src} → {dst}")
            else:
                # 确保目标目录存在
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                
                # 移动文件
                shutil.move(str(src_path), str(dst_path))
                print(f"  ✓ {src} → {dst}")
            
            self.migration_log.append(f"移动文件: {src} → {dst}")
    
    def update_imports(self):
        """更新导入路径"""
        print("\n🔄 更新导入路径...")
        
        # 需要更新的文件
        files_to_update = [
            'main.py',
            'config.py',
            'scripts/auto_screen_and_analyze.py',
            'scripts/verify_integration.py',
            'scripts/test_integration.py',
            'core/*.py',
            'screeners/*.py',
            'services/*.py',
        ]
        
        updated_count = 0
        
        for pattern in files_to_update:
            for file_path in self.project_root.glob(pattern):
                if file_path.is_file() and file_path.suffix == '.py':
                    if self._update_file_imports(file_path):
                        updated_count += 1
        
        print(f"  ✓ 更新了 {updated_count} 个文件")
        self.migration_log.append(f"更新导入路径: {updated_count} 个文件")
    
    def _update_file_imports(self, file_path: Path) -> bool:
        """更新单个文件的导入"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            original_content = content
            
            # 应用替换规则
            for old_import, new_import in self.IMPORT_REPLACEMENTS.items():
                content = content.replace(old_import, new_import)
            
            # 如果有变化
            if content != original_content:
                if not self.dry_run:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                print(f"  ✓ 更新: {file_path.relative_to(self.project_root)}")
                return True
            
            return False
            
        except Exception as e:
            print(f"  ⚠️  更新失败 {file_path}: {e}")
            return False
    
    def create_init_files(self):
        """创建 __init__.py 文件"""
        print("\n📝 创建 __init__.py 文件...")
        
        init_contents = {
            'core/__init__.py': '''"""核心业务模块"""

from .analyzer import GeminiAnalyzer, AnalysisResult
from .stock_analyzer import StockTrendAnalyzer, TrendAnalysisResult
from .market_analyzer import MarketAnalyzer
from .storage import get_db, DatabaseManager

__all__ = [
    'GeminiAnalyzer', 'AnalysisResult',
    'StockTrendAnalyzer', 'TrendAnalysisResult',
    'MarketAnalyzer',
    'get_db', 'DatabaseManager'
]
''',
            'screeners/__init__.py': '''"""选股模块 - 整合多种选股策略"""

from .stock_screener import StockScreener, ScreeningMode
from .strategy_screener import StrategyScreener

__all__ = ['StockScreener', 'ScreeningMode', 'StrategyScreener']
''',
            'services/__init__.py': '''"""服务模块"""

from .notification import NotificationService
from .search_service import SearchService
from .scheduler import run_with_schedule

__all__ = ['NotificationService', 'SearchService', 'run_with_schedule']
''',
        }
        
        for file_path, content in init_contents.items():
            full_path = self.project_root / file_path
            if self.dry_run:
                print(f"  [预览] 创建: {file_path}")
            else:
                with open(full_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                print(f"  ✓ 创建: {file_path}")
            
            self.migration_log.append(f"创建 __init__.py: {file_path}")
    
    def save_migration_log(self):
        """保存迁移日志"""
        log_file = self.project_root / 'migration_log.json'
        
        log_data = {
            'timestamp': datetime.now().isoformat(),
            'dry_run': self.dry_run,
            'backup_dir': str(self.backup_dir),
            'operations': self.migration_log
        }
        
        if not self.dry_run:
            with open(log_file, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
            print(f"\n📋 迁移日志已保存: {log_file}")
    
    def run(self):
        """执行重构"""
        print("=" * 60)
        if self.dry_run:
            print("🔍 预览模式 - 不会实际修改文件")
        else:
            print("🚀 开始项目结构重构")
        print("=" * 60)
        print()
        
        try:
            # 1. 备份
            self.backup_project()
            
            # 2. 创建目录
            self.create_directories()
            
            # 3. 移动文件
            self.move_files()
            
            # 4. 创建 __init__.py
            self.create_init_files()
            
            # 5. 更新导入
            self.update_imports()
            
            # 6. 保存日志
            self.save_migration_log()
            
            print("\n" + "=" * 60)
            if self.dry_run:
                print("✅ 预览完成")
                print("\n执行重构请运行: python refactor_structure.py")
            else:
                print("✅ 重构完成！")
                print(f"\n备份位置: {self.backup_dir}")
                print("\n下一步:")
                print("  1. 运行测试: python -m pytest tests/")
                print("  2. 验证功能: python scripts/test_integration.py")
                print("  3. 如有问题，可以回滚: python refactor_structure.py --rollback")
            print("=" * 60)
            
            return True
            
        except Exception as e:
            print(f"\n❌ 重构失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def rollback(self):
        """回滚到备份"""
        print("🔄 开始回滚...")
        
        # 查找最新的备份
        backups = sorted(self.project_root.glob('.backup_*'))
        if not backups:
            print("❌ 未找到备份目录")
            return False
        
        latest_backup = backups[-1]
        print(f"📦 使用备份: {latest_backup}")
        
        # 恢复文件
        for backup_file in latest_backup.glob('*'):
            if backup_file.is_file():
                target = self.project_root / backup_file.name
                shutil.copy2(backup_file, target)
                print(f"  ✓ 恢复: {backup_file.name}")
        
        print("✅ 回滚完成")
        return True


def main():
    parser = argparse.ArgumentParser(
        description='项目结构重构脚本',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='预览模式，不实际修改文件'
    )
    
    parser.add_argument(
        '--rollback',
        action='store_true',
        help='回滚到备份'
    )
    
    args = parser.parse_args()
    
    # 获取项目根目录
    project_root = Path(__file__).parent
    
    # 创建重构器
    refactor = ProjectRefactor(project_root, dry_run=args.dry_run)
    
    # 执行
    if args.rollback:
        success = refactor.rollback()
    else:
        success = refactor.run()
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
