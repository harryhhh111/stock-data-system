"""Explicit research entries, immutable Git snapshots and fail-closed diagnostics."""
import pytest

from web.services.research_reader import GitSnapshot, ResearchReadError, extract_section, read_research


def test_match_chinese_folder_and_committed_sections(research_repo):
    repo = research_repo
    repo.entry(folder='Nebius公司研究', code='NBIS', entity='company:nebius', mic='XNAS')
    sha = repo.commit()
    repo.write('Nebius公司研究/主文档.md', '未提交：不能发送给模型')
    repo.entry(folder='untracked', code='FAKE', entity='company:fake')
    docs, text, diagnostic = read_research(repo.root, 'US', 'nbis')
    assert docs == ['Nebius公司研究/主文档.md']
    assert 'OCI 增速' in text
    assert all(s not in text for s in ['简介不是观点', '未提交', '不应进入摘录'])
    assert diagnostic['commit_sha'] == sha
    assert diagnostic['entity_id'] == 'company:nebius'
    assert diagnostic['sections'][0]['line'] == 3
    assert diagnostic['missing_sections'] == ['risks', 'metrics', 'open_questions']
    assert read_research(repo.root, 'US', 'FAKE')[2]['status'] == 'no_entry'


def test_snapshot_does_not_follow_head_move(research_repo):
    repo = research_repo
    repo.entry()
    sha = repo.commit()
    snapshot = GitSnapshot(repo.root)
    repo.write('ORCL/主文档.md', '# Changed\n新版本')
    repo.commit()
    assert snapshot.commit == sha
    assert 'OCI 增速' in snapshot.read('ORCL/主文档.md')


def test_cross_market_and_leading_zero(research_repo):
    repo = research_repo
    repo.entry('us', '00700', 'company:us')
    repo.entry('hk', '00700', 'company:hk', 'HK', 'XHKG')
    repo.entry('cn', '000001', 'company:cn', 'CN', 'XSHE')
    repo.commit()
    assert read_research(repo.root, 'US', '00700')[2]['entity_id'] == 'company:us'
    assert read_research(repo.root, 'CN_HK', '00700')[2]['entity_id'] == 'company:hk'
    assert read_research(repo.root, 'CN_A', '000001')[2]['entity_id'] == 'company:cn'


def test_missing_security_and_topic_are_not_guessed(research_repo):
    repo = research_repo
    repo.entry('SSD', code=None, entity='topic:nand')
    repo.write('NBIS/NBIS.md', '只有文件夹，不是入口')
    repo.commit()
    for code in ['NAND', 'NBIS']:
        docs, text, info = read_research(repo.root, 'US', code)
        assert (docs, text, info['status']) == ([], '', 'no_entry')


def test_section_budgets_and_diagnostics(research_repo):
    repo = research_repo
    data = repo.entry(text='# Report\n## 观点\n' + '论点' * 1200 + '\n## 风险\n风险需要独立保留。\n')
    data['sections']['risks'] = dict(path='主文档.md', heading_path=['Report', '风险'])
    repo.yaml('ORCL', data)
    repo.commit()
    _, text, info = read_research(repo.root, 'US', 'ORCL')
    assert '风险需要独立保留' in text
    assert info['sections'][0]['truncated'] is True
    assert info['sections'][1]['truncated'] is False
    assert '已截断' in text


def test_all_four_sections_share_a_bounded_budget(research_repo):
    repo = research_repo
    data = repo.entry(text='# Report\n' + ''.join(f'## {key}\n' + key * 2000 + '\n'
                                                for key in ['thesis', 'risks', 'metrics', 'open_questions']))
    for key in ['thesis', 'risks', 'metrics', 'open_questions']:
        data['sections'][key] = dict(path='主文档.md', heading_path=['Report', key])
    repo.yaml('ORCL', data)
    repo.commit()
    _, text, info = read_research(repo.root, 'US', 'ORCL')
    assert len(info['sections']) == 4
    assert all(s['truncated'] for s in info['sections'])
    assert not info['missing_sections']
    # 6000 body characters plus explicit source labels, not 6000 per document.
    assert len(text) < 6600


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(schema_version=1),
    lambda d: d.update(schema_version=True),
    lambda d: d.update(unexpected=True),
    lambda d: d['securities'][0].update(market='UNKNOWN'),
    lambda d: d['securities'][0].update(mic='XXXX'),
    lambda d: d['securities'][0].update(market='HK'),
    lambda d: d['securities'][0].update(symbol=700),
    lambda d: d['securities'][0].pop('market'),
    lambda d: d.update(primary_document='/etc/passwd'),
    lambda d: d.update(primary_document='../../outside.md'),
    lambda d: d.update(primary_document='missing.md'),
    lambda d: d.update(primary_document='..\\outside.md'),
    lambda d: d['sections']['thesis'].update(heading_path=['Report', '不存在']),
    lambda d: d['sections']['thesis'].update(path='未声明.md'),
    lambda d: d.update(related_entities=[dict(entity_id='topic:missing', relation='related')]),
    lambda d: d['securities'].append(d['securities'][0].copy()),
])
def test_invalid_entries_fail_closed(research_repo, mutation):
    repo = research_repo
    data = repo.entry()
    repo.write('ORCL/未声明.md', '# Report\n## 观点\n不能自动读取')
    mutation(data)
    repo.yaml('ORCL', data)
    repo.commit()
    with pytest.raises(ResearchReadError):
        read_research(repo.root, 'US', 'UNKNOWN')


@pytest.mark.parametrize('raw', ['schema_version: 2\nschema_version: 2',
                                  'root: &x [1, 2]\nalias: *x', '!!python/object:builtins.object {}', '[bad'])
def test_yaml_duplicate_alias_tag_and_syntax(research_repo, raw):
    repo = research_repo
    repo.write('bad/research.yaml', raw)
    repo.commit()
    with pytest.raises(ResearchReadError):
        read_research(repo.root, 'US', 'ORCL')


@pytest.mark.parametrize('same_entity,same_mic', [(True, False), (False, True), (False, False)])
def test_duplicate_subject_security_or_ambiguous_market(research_repo, same_entity, same_mic):
    repo = research_repo
    repo.entry('first')
    repo.entry('second', entity='company:oracle' if same_entity else 'company:other',
               mic='XNYS' if same_mic else 'XNAS')
    repo.commit()
    with pytest.raises(ResearchReadError, match='重复|歧义'):
        read_research(repo.root, 'US', 'ORCL')


def test_symlink_blob_rejected_but_worktree_symlink_never_read(research_repo, tmp_path):
    repo = research_repo
    repo.entry()
    repo.commit()
    target = repo.root / 'ORCL/主文档.md'
    target.unlink()
    outside = tmp_path / 'outside.md'
    outside.write_text('外部敏感数据')
    target.symlink_to(outside)
    assert '外部敏感数据' not in read_research(repo.root, 'US', 'ORCL')[1]
    repo.commit()
    with pytest.raises(ResearchReadError, match='符号链接'):
        read_research(repo.root, 'US', 'ORCL')


def test_symlink_entry_rejected(research_repo):
    repo = research_repo
    repo.entry()
    path = repo.root / 'ORCL/research.yaml'
    path.unlink()
    path.symlink_to('主文档.md')
    repo.commit()
    with pytest.raises(ResearchReadError, match='符号链接'):
        read_research(repo.root, 'US', 'ORCL')


def test_unrelated_invalid_entry_blocks_partial_result(research_repo):
    repo = research_repo
    repo.entry()
    repo.write('broken/research.yaml', 'schema_version: 1')
    repo.commit()
    with pytest.raises(ResearchReadError, match='broken/research.yaml'):
        read_research(repo.root, 'US', 'ORCL')


def test_no_sections_is_not_generic(research_repo):
    repo = research_repo
    data = repo.entry()
    data['sections']['thesis'] = None
    repo.yaml('ORCL', data)
    repo.commit()
    with pytest.raises(ResearchReadError, match='没有配置'):
        read_research(repo.root, 'US', 'ORCL')


def test_fences_exact_ancestors_and_duplicate_headings():
    text = '# Root\n```markdown\n## Idea\n假的观点\n```\n## Idea ###\n真实观点\n## End\n其他内容'
    body, line = extract_section(text, ['Root', 'Idea'])
    assert body == '## Idea ###\n真实观点' and line == 6
    with pytest.raises(ResearchReadError, match='不存在'):
        extract_section(text, ['Idea'])
    with pytest.raises(ResearchReadError, match='不唯一'):
        extract_section(text + '\n## Idea\n重复观点', ['Root', 'Idea'])
    with pytest.raises(ResearchReadError, match='正文为空'):
        extract_section('# Root\n## Idea\n## End\n', ['Root', 'Idea'])


def test_unavailable_or_non_repository(research_repo, tmp_path):
    for root in [tmp_path / 'missing', tmp_path]:
        with pytest.raises(ResearchReadError):
            read_research(root, 'US', 'ORCL')
    with pytest.raises(ResearchReadError):
        read_research(research_repo.root, 'US', 'ORCL')  # No HEAD commit yet.


def test_excluded_reference_tree_is_not_loaded(research_repo):
    repo = research_repo
    repo.write('参考库/untrusted/research.yaml', 'not valid YAML')
    repo.commit()
    assert read_research(repo.root, 'US', 'ORCL')[2]['status'] == 'no_entry'
