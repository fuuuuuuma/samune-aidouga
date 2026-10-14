"""Explainable, local retrieval; selection is not approval or image generation."""
from __future__ import annotations
import json, re, unicodedata
from pathlib import Path
from background_library import read, write, now, digest, local_path, ZONES

ROLES={'base','texture','overlay','accent'}
MODES={'library','provided_scene','provided_image','locked','none'}
ALIASES={'サイバー':['デジタル','ネオン','近未来','回路'], 'AI':['デジタル','データ','近未来'], '自動化':['データ','回路'], '時短':['スピード','流線'], '爆速':['スピード','集中線'], '上品':['高級感','シルク'], '優しい':['柔らかい','パステル'], 'やさしい':['柔らかい','パステル'], '高級':['高級感','金','シルク'], 'ピンク':['桃'], 'ゴールド':['金'], 'ブルー':['青'], 'シンプル':['余白','低密度'], '静か':['余白','低密度'], '明るい':['明るめ'], '暗い':['暗め'], '黒背景':['黒','暗め']}

def norm(value):return unicodedata.normalize('NFKC',value).lower()
def area(box):return max(0,box[2]-box[0])*max(0,box[3]-box[1])
def overlap(a,b):return area([max(a[0],b[0]),max(a[1],b[1]),min(a[2],b[2]),min(a[3],b[3])])
def validate_brief(b):
    if b.get('phase','initial') not in ('initial','revision'):raise ValueError('phase must be initial or revision')
    if b.get('role','base') not in ROLES:raise ValueError('Unknown role')
    if b.get('base_mode','library') not in MODES:raise ValueError('Unknown base_mode')
    for key in ('text_regions','face_regions'):
        for r in b.get(key,[]):
            box=r.get('bbox')
            if not isinstance(box,list) or len(box)!=4 or not all(isinstance(x,(int,float)) and 0<=x<=1 for x in box) or area(box)<=0:raise ValueError('bbox must be normalized [left,top,right,bottom]')

def language_terms(brief,assets):
    q=norm(brief.get('query','')); terms=set(map(norm,brief.get('concepts',[])))
    vocabulary={t for a in assets for t in a.get('tags',[])}|set(ZONES)|{'明るめ','暗め','低密度','中密度','高密度'}
    terms.update(norm(t) for t in vocabulary if len(t)>1 and norm(t) in q)
    for word,alternatives in ALIASES.items():
        if norm(word) in q:terms.update(map(norm,alternatives))
    terms.update(t for t in re.split(r'[\s,、]+',q) if t)
    return sorted(terms)

def region_metrics(a,box):
    cells=[(overlap(box,c['bbox']),c) for c in a['grid']];total=sum(w for w,c in cells)
    return {k:sum(w*c[k] for w,c in cells)/total for k in ('luma','quiet_score','highlight_fraction','edge')}

def rank_assets(assets,brief,used_hashes=()):
    """Hard constraints first, heuristic ordering second. No score means CTR."""
    validate_brief(brief);phase=brief.get('phase','initial');mode=brief.get('base_mode','library');role=brief.get('role','base')
    common={'created_at':now(),'brief':brief,'scoring':'heuristic_not_ctr_probability','candidates':[], 'proposed_asset':None,'visual_selection_required':True}
    if mode=='none':return common|{'action':'none','reason':'ラフに装飾背景を追加しない指定。'}
    if mode=='locked' or (phase=='revision' and not brief.get('background_change_requested',False)):
        return common|{'action':'preserve','locked_reference':brief.get('existing_background'), 'reason':'背景変更の指示がない限定修正。既存背景を維持し再選択しない。','visual_selection_required':False}
    if role=='base' and mode in ('provided_scene','provided_image'):
        return common|{'action':'use_provided','provided_reference':brief.get('provided_reference'),'reason':'指定された場面・支給画像を背景に使う。素材集で置換しない。','visual_selection_required':False}
    terms=language_terms(brief,assets);used=set(used_hashes)|set(brief.get('exclude_hashes',[]));ranked=[]
    for a in assets:
        if a.get('visual_status')!='reviewed' or role not in a.get('roles',[]):continue
        if role=='base' and a['sha256'] in used:continue
        if brief.get('categories') and a['category'] not in brief['categories']:continue
        if brief.get('lighting') and a['lighting']!=brief['lighting']:continue
        if brief.get('max_density')=='低密度' and a['density']!='低密度':continue
        if brief.get('min_width',0)>a['width']:continue
        if role=='base' and not 1.55<=a['aspect_ratio']<=2.05:continue
        hay=norm(' '.join([a.get('visual_description',''),*a.get('tags',[]),*a['color_names'],a['lighting'],a['density']]))
        if any(norm(t) in hay for t in brief.get('avoid',[])):continue
        if any(norm(t) not in hay for t in brief.get('required_terms',[])):continue
        # Known topical imagery must be explicitly justified by a semantic match.
        restricted={'国旗','果物','レモン','髪','コラージュ','クロマキー'}&set(a.get('tags',[]))
        if restricted and not any(norm(t) in norm(brief.get('query','')) or norm(t) in set(map(norm,brief.get('concepts',[]))) for t in restricted):continue
        matched=[t for t in terms if t in hay]
        # Terms are suggestions; required_terms expresses a non-negotiable concept.
        score=min(42,len(matched)*9);reasons=[];risks=[a.get('caution','')]
        if matched:reasons.append('意味・雰囲気: '+ ' / '.join(matched))
        palette=brief.get('palette',[]);colors=[c for c in palette if c in a['color_names'] or c in a.get('tags',[])]
        if colors:score+=min(18,len(colors)*9);reasons.append('希望配色: '+'・'.join(colors))
        checks=[]
        for r in brief.get('text_regions',[]):
            m=region_metrics(a,r['bbox']);light=r.get('text_tone','light')=='light';contrast=(255-m['luma'])/255 if light else m['luma']/255
            contribution=m['quiet_score']/100*12+contrast*12-m['highlight_fraction']*6 if light else m['quiet_score']/100*12+contrast*12
            checks.append((contribution,m,r))
        if checks:
            score+=sum(x[0] for x in checks)/len(checks)
            reasons.append('指定文字領域の静けさ '+str(round(sum(m['quiet_score'] for _,m,_ in checks)/len(checks)))+'/100（計測）')
            for _,m,r in checks:
                if m['quiet_score']<55 or (r.get('text_tone','light')=='light' and m['luma']>160):risks.append(r.get('name','文字領域')+'に模様や明るさの競合。原寸で確認。')
        else:score+=min(12,max(c['quiet_score'] for c in a['grid'])/100*12)
        for r in brief.get('face_regions',[]):
            m=region_metrics(a,r['bbox']);score-=m['highlight_fraction']*10+m['edge']/10
            if m['highlight_fraction']>.25:risks.append('人物の位置に強い光。顔の輪郭と競合する可能性。')
        score+=min(6,a['width']/3840*6)
        if not a['native_4k']:risks.append(f"素材は{a['width']}×{a['height']}。4K納品では拡大後の見え方を確認。")
        if a.get('description_scope')=='family_plus_measurements':risks.append('形の説明は4枚共通所見を含む。候補単体を必ず開く。')
        ranked.append({'asset_id':a['id'],'sha256':a['sha256'],'path':a['absolute_path'],'image_path':a['image_path'],'thumbnail_path':a['thumbnail_path'],'post_key':a['post_key'],'score':round(score,2),'reasons':reasons,'risks':list(dict.fromkeys(x for x in risks if x)), 'description':a['description'],'width':a['width'],'height':a['height']})
    ranked.sort(key=lambda x:(-x['score'],x['asset_id']));seen=set();result=[]
    for x in ranked:
        if x['sha256'] in seen:continue
        seen.add(x['sha256']);result.append(x)
    return common|{'action':'choose' if result else 'no_match','reason':'重複を除いた候補。原寸とラフで目視比較してAIが最終選択。' if result else '条件を満たす素材なし。指定場面を維持し、条件を勝手に緩めない。','query_terms':terms,'candidates':result,'proposed_asset':result[0]['asset_id'] if result else None}

def integrity(root,full=False):
    root=Path(root).resolve();inv=read(root/'catalog/inventory.json');known={a['image_path']:a for a in inv['assets']};changed=[];missing=[]
    for rel,a in known.items():
        p=local_path(root,rel)
        if not p.exists():missing.append(rel);continue
        s=p.stat()
        if s.st_size!=a['bytes'] or s.st_mtime_ns!=a['mtime_ns'] or full:
            if digest(p)!=a['sha256']:changed.append(rel)
    actual={str(p.relative_to(root)) for p in (root/'images').rglob('*') if p.is_file() and p.suffix.lower() in {'.jpg','.jpeg','.png','.webp','.tif','.tiff'}}
    return {'checked_at':now(),'full_hash_check':full,'indexed':len(known),'added':sorted(actual-set(known)),'changed':changed,'missing':missing,'unchanged':not(changed or missing or actual-set(known))}

def reviewed_notes(root):
    notes={}
    for line in (root/'review/visual-notes.tsv').read_text().splitlines():
        if not line.strip() or line.startswith('#'):continue
        cols=line.split('\t');key=cols[0]
        if key in notes:raise ValueError('Duplicate note '+key)
        if len(cols)==2 and cols[1].startswith('='):
            notes[key]=dict(notes[cols[1][1:]])
        elif len(cols)==5:
            notes[key]={'visual_description':cols[1],'roles':cols[2].split(','),'tags':cols[3].split(','),'caution':cols[4]}
        else:raise ValueError('Malformed review '+key)
    return notes

def finalize(root):
    root=Path(root).resolve();inv=read(root/'catalog/inventory.json');notes=reviewed_notes(root)
    check=integrity(root,True)
    if not check['unchanged']:raise ValueError('Inventory differs from files. Inspect check output before rebuilding.')
    missing={a['post_key'] for a in inv['assets']}-set(notes)
    if missing:raise ValueError('Unreviewed posts: '+','.join(sorted(missing)))
    overrides=read(root/'review/variant-notes.json') if (root/'review/variant-notes.json').exists() else {}
    evidence_path=root/'review/notes-evidence.json';evidence=read(evidence_path) if evidence_path.exists() else {}
    override_by_hash={}
    for a in inv['assets']:
        key=a['post_key']+'-'+a['id'].rsplit('-',1)[1]
        if key in overrides:override_by_hash[a['sha256']]=overrides[key]
    for a in inv['assets']:
        if a['id'] in evidence and evidence[a['id']]!=a['sha256']:raise ValueError('Stale visual review '+a['id']+'; re-review and rebind evidence explicitly.')
        a.update(notes[a['post_key']]);a['family_description']=a['visual_description'];a['description_scope']='family_plus_measurements'
        if a['sha256'] in override_by_hash:
            a.update(override_by_hash[a['sha256']]);a['description_scope']='individual_plus_measurements'
        a['visual_status']='reviewed';a['review_method']='312×176 tiles in 32-image contact sheets; candidate native inspection required at production'
        a['description']=a['visual_description']+' '+a['measurement_caption']+f" 元画像{a['width']}×{a['height']}。"
    evidence={a['id']:a['sha256'] for a in inv['assets']};write(evidence_path,evidence)
    inv['reviewed_at']=now();inv['reviewed_image_count']=len(inv['assets']);write(root/'catalog/catalog.json',inv)
    idx=read(root/'catalog/review-index.json')
    for page in idx['pages']:
        page['viewed']=True;page['notes_source']='review/visual-notes.tsv';page['notes']=[notes[k]['visual_description'] for k in page['post_keys']]
    idx['review_is_complete']=True;idx['recorded_at']=now();idx['limits']='一覧目視の記録。全画像の原寸・全用途への適合確認ではない。';write(root/'catalog/review-index.json',idx)
    compact=[]
    keep=('id','post_key','category','category_name','absolute_path','image_path','thumbnail_path','sha256','width','height','lighting','density','color_names','palette','quiet_zones','brightest_zone','roles','tags','description','caution','source_url','source_text','duplicate_ids','description_scope')
    for a in inv['assets']:compact.append({k:a[k] for k in keep})
    payload={'count':len(compact),'unique':inv['unique_byte_hash_count'],'categories':inv['categories'],'reviewed_at':inv['reviewed_at'],'assets':compact}
    js='window.BACKGROUNDS = '+json.dumps(payload,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')+';\n'
    (root/'catalog/search-data.js').write_text(js)
    write(root/'catalog/verification.json',check)
    return {'reviewed':len(compact),'unique':inv['unique_byte_hash_count'],'pages':len(idx['pages']),'native_4k':sum(a['native_4k'] for a in inv['assets']),'individual_variant_notes':sum(a['description_scope']=='individual_plus_measurements' for a in inv['assets'])}

def search(root,brief,limit=8):
    root=Path(root).resolve();state=integrity(root)
    if state['changed'] or state['missing']:raise ValueError('Catalog has modified or missing assets; run check --full and re-review.')
    assets=read(root/'catalog/catalog.json')['assets'];used=[]
    inventory={a['id']:a['sha256'] for a in read(root/'catalog/inventory.json')['assets']}
    if any(inventory.get(a['id'])!=a['sha256'] for a in assets):raise ValueError('Published catalog review is stale; re-review changed assets and finalize.')
    history=root/'catalog/usage.jsonl'
    if history.exists():
        for line in history.read_text().splitlines():
            row=json.loads(line)
            if row['role']=='base' and row.get('channel_id')==brief.get('channel_id') and row['status'] in ('used','approved'):used.append(row['sha256'])
    result=rank_assets(assets,brief,used);result['candidates']=result['candidates'][:limit];result['new_unreviewed_files']=state['added']
    for c in result['candidates']:
        if digest(c['path'])!=c['sha256']:raise ValueError('Candidate changed: '+c['asset_id'])
    return result

def select(root,selection_path,asset_id,reason,viewed_ids,transforms=None):
    root=Path(root).resolve();result=read(selection_path)
    if result['action']!='choose':raise ValueError('This brief must preserve/use provided/skip background')
    c=next((x for x in result['candidates'] if x['asset_id']==asset_id),None)
    if not c or asset_id not in viewed_ids:raise ValueError('Selection must be a candidate actually viewed')
    if not reason.strip():raise ValueError('Selection reason required')
    if digest(c['path'])!=c['sha256']:raise ValueError('Selected image changed')
    result['selected']={**c,'reason':reason,'transforms':transforms or {},'viewed_candidate_ids':viewed_ids,'selected_at':now(),'selected_by':'assistant_after_visual_review','approved_by_user':False}
    result['visual_selection_required']=False;write(selection_path,result);return result['selected']

def record_use(root,selection_path,project,variant,status):
    root=Path(root).resolve();r=read(selection_path);s=r.get('selected')
    if not s:raise ValueError('No visually selected asset; proposals are not usage')
    if digest(s['path'])!=s['sha256']:raise ValueError('Selected image changed')
    if status not in ('used','approved'):raise ValueError('Unknown status')
    row={'at':now(),'project':project,'variant':variant,'channel_id':r['brief'].get('channel_id'),'role':r['brief'].get('role','base'),'asset_id':s['asset_id'],'sha256':s['sha256'],'status':status}
    with (root/'catalog/usage.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    return row
