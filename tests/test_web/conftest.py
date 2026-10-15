"""Committed research fixtures: no live DB, models, or external repository writes."""
import subprocess

import pytest
import yaml


class ResearchRepo:
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.git('init', '-q', '--template=', '-b', 'main')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test',
                               '-c', 'user.email=test@example.com', '-c', 'commit.gpgsign=false',
                               '-c', 'core.hooksPath=/dev/null', *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def entry(self, folder='ORCL', code='ORCL', entity='company:oracle',
              market='US', mic='XNYS', text=None):
        data = dict(schema_version=2, entity_id=entity, kind=entity.split(':')[0], name='Research',
                    aliases=[], securities=[dict(market=market, mic=mic, symbol=code)] if code else [],
                    primary_document='主文档.md', documents=[], related_entities=[],
                    sections={key: None for key in ['thesis', 'risks', 'metrics', 'open_questions', 'research_metadata']})
        data['sections']['thesis'] = dict(path='主文档.md', heading_path=['Report', '观点'])
        self.write(f'{folder}/主文档.md', text or '# Report\n简介不是观点。\n## 观点\nOCI 增速与 RPO 是核心变量，待验证电力约束。\n## 其他\n不应进入摘录。\n')
        self.yaml(folder, data)
        return data

    def yaml(self, folder, data):
        self.write(f'{folder}/research.yaml', yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-q', '--allow-empty', '-m', 'Research fixture')
        return self.git('rev-parse', 'HEAD')


@pytest.fixture
def research_repo(tmp_path):
    return ResearchRepo(tmp_path / 'stock-research')
