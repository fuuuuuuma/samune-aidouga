#!/usr/bin/env python3
"""Local background inventory, visual review, explainable retrieval and usage records."""
from __future__ import annotations
import argparse, colorsys, hashlib, json, math, re, shutil, sys, unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

ZONES = ['左上','上中央','右上','左中央','中央','右中央','左下','下中央','右下']
COLORS = ['赤','橙','黄','緑','青緑','青','紫','桃']
def now(): return datetime.now().astimezone().isoformat(timespec='seconds')
def read(p): return json.loads(Path(p).read_text())
def write(p, data):
    p=Path(p); p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n'); tmp.replace(p)
def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''): h.update(block)
    return h.hexdigest()
def local_path(root, relative):
    p=(root/relative).resolve()
    if not p.is_relative_to(root.resolve()): raise ValueError('Path outside background library')
    return p
def color_name(rgb):
    r,g,b=[float(x)/255 for x in rgb];h,s,v=colorsys.rgb_to_hsv(r,g,b)
    if v<.16:return '黒'
    if s<.14:return '白' if v>.83 else '灰'
    if .06<h<.15 and v<.57:return '茶'
    for upper,name in [(15,'赤'),(45,'橙'),(70,'黄'),(165,'緑'),(195,'青緑'),(255,'青'),(290,'紫'),(345,'桃'),(361,'赤')]:
        if h*360<upper:return name
def measure(task):
    root,item,photo,number,cat=task;path=local_path(root,photo['img'])
    with Image.open(path) as source:
        im=ImageOps.exif_transpose(source);size=list(im.size)
        rgba=im.convert('RGBA');alpha=rgba.getchannel('A').getextrema()[0]<255
        small=rgba.convert('RGB').resize((240,135),Image.Resampling.BILINEAR)
        rgb=np.asarray(small).astype(float);gray=rgb@np.array([.2126,.7152,.0722])
        grad=np.zeros(gray.shape);grad[:,1:]+=abs(np.diff(gray,axis=1));grad[1:,:]+=abs(np.diff(gray,axis=0))
        cells=[]
        for row in range(3):
            for col in range(3):
                a=gray[row*45:(row+1)*45,col*80:(col+1)*80];g=grad[row*45:(row+1)*45,col*80:(col+1)*80]
                cells.append({'zone':ZONES[row*3+col],'bbox':[col/3,row/3,(col+1)/3,(row+1)/3], 'luma':round(float(a.mean()),1),'luma_std':round(float(a.std()),1),'edge':round(float(g.mean()),1),'highlight_fraction':round(float((a>220).mean()),3),'quiet_score':round(float(max(0,100-g.mean()*2-a.std()*.4)),1)})
        quant=small.quantize(colors=8);pal=quant.getpalette();color_counts=[]
        for count,idx in sorted(quant.getcolors(),reverse=True):
            vals=pal[idx*3:idx*3+3];color_counts.append({'hex':'#'+''.join(f'{v:02x}' for v in vals),'name':color_name(vals),'fraction':round(count/(240*135),3)})
    combined=Counter()
    for x in color_counts:combined[x['name']]+=x['fraction']
    quiet=sorted(cells,key=lambda x:-x['quiet_score'])[:3]
    brightest=max(cells,key=lambda x:x['luma']);darkest=min(cells,key=lambda x:x['luma'])
    luma=float(gray.mean());edge=float(grad.mean());lighting='暗め' if luma<75 else ('明るめ' if luma>170 else '中明度')
    density='低密度' if edge<8 else ('中密度' if edge<22 else '高密度')
    names=[k for k,_ in combined.most_common(3)]
    return {'id':f'bg-{item["id"]}-{number}','post_key':item['key'],'source_post_id':item['id'],'category':item['cat'],'category_name':cat['name'],'source_text':item['text'],'source_url':item.get('source_url') or item.get('url'),'date':item['date'],'image_path':photo['img'],'thumbnail_path':photo['thumb'],'absolute_path':str(path),'sha256':digest(path),'bytes':path.stat().st_size,'mtime_ns':path.stat().st_mtime_ns,'width':size[0],'height':size[1],'alpha':bool(alpha),'native_4k':size[0]>=3840 and size[1]>=2160,'aspect_ratio':round(size[0]/size[1],4),'palette':color_counts,'color_names':names,'lighting':lighting,'density':density,'luma':round(luma,1),'edge':round(edge,1),'grid':cells,'quiet_zones':[x['zone'] for x in quiet],'brightest_zone':brightest['zone'],'darkest_zone':darkest['zone'],'measurement_caption':f'{"・".join(names)}を含む{lighting}の素材。模様の密度は{density}。9分割計測では{brightest["zone"]}が最も明るく、{darkest["zone"]}が最も暗い。比較的静かな領域の候補は{"・".join(x["zone"] for x in quiet)}。','measurement_limits':'色名と静けさは240×135の計測による候補。余白、文字可読性、光源の意味を保証しない。原寸とラフの配置で確認。','visual_status':'pending'}

def build(root):
    root=Path(root).resolve();catalog=root/'catalog';catalog.mkdir(exist_ok=True)
    source=catalog/'source-index.json'
    if source.exists(): data=read(source)
    else:
        original=(root/'index.html').read_text();i=original.index('const DATA');start=original.index('{',i)
        data,_=json.JSONDecoder().raw_decode(original[start:]);write(source,data)
        backup=root/'archive'/'index-before-background-search.html';backup.parent.mkdir(exist_ok=True)
        if not backup.exists():shutil.copy2(root/'index.html',backup)
    categories={x['id']:x for x in data['cats']};order={x['id']:i for i,x in enumerate(data['cats'])}
    known={p['img'] for item in data['items'] for p in item['photos']}
    folder_categories={str(Path(p['img']).parent):item['cat'] for item in data['items'] for p in item['photos']}
    for path in sorted((root/'images').rglob('*')):
        if not path.is_file() or path.suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.tif','.tiff'}:continue
        rel=str(path.relative_to(root))
        if rel in known:continue
        cat=folder_categories.get(str(Path(rel).parent),path.parent.name)
        if cat not in categories:
            cat='uncategorized'
            if cat not in categories:
                c={'id':cat,'name':'追加・未分類'};data['cats'].append(c);categories[cat]=c;order[cat]=len(order)
        data['items'].append({'id':'local-'+hashlib.sha256(rel.encode()).hexdigest()[:20],'cat':cat,'date':datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d'),'text':'ローカル追加: '+path.name,'photos':[{'img':rel,'thumb':rel}]})
    old=read(catalog/'posts.json') if (catalog/'posts.json').exists() else []
    keys={x['id']:x['key'] for x in old};next_key=max([int(k[1:]) for k in keys.values()]+[0])+1
    posts=sorted(data['items'],key=lambda x:(order[x['cat']],x['date'],x['id']))
    for item in posts:
        if item['id'] not in keys:keys[item['id']]=f'P{next_key:03}';next_key+=1
        item['key']=keys[item['id']]
    posts.sort(key=lambda x:int(x['key'][1:]))
    tasks=[]
    for n,item in enumerate(posts,1):
        for i,photo in enumerate(item['photos'],1):tasks.append((root,item,photo,i,categories[item['cat']]))
    write(source,data)
    with ThreadPoolExecutor(max_workers=6) as pool: assets=list(pool.map(measure,tasks))
    groups={}
    for a in assets:groups.setdefault(a['sha256'],[]).append(a['id'])
    for a in assets:a['duplicate_ids']=[x for x in groups[a['sha256']] if x!=a['id']]
    write(catalog/'inventory.json',{'schema_version':1,'created_at':now(),'root':str(root),'categories':data['cats'],'post_count':len(posts),'image_count':len(assets),'unique_byte_hash_count':len(groups),'assets':assets})
    write(catalog/'posts.json',posts)
    review=root/'review';review.mkdir(exist_ok=True)
    font_path=Path('/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc')
    font=ImageFont.truetype(str(font_path),14) if font_path.exists() else ImageFont.load_default()
    pages=[]
    for first in range(0,len(posts),8):
        rows=posts[first:first+8];page_no=first//8+1
        sheet=Image.new('RGB',(1280,len(rows)*224+36),'#111827');draw=ImageDraw.Draw(sheet)
        draw.text((8,8),f'Background review {page_no:02} / {(len(posts)+7)//8} — all 4 variants per post',font=font,fill='white')
        ids=[]
        for row,item in enumerate(rows):
            y=36+row*224;title=f'{item["key"]} {categories[item["cat"]]["name"]} ｜ '+re.sub(r'\s+',' ',item['text'])
            draw.text((8,y),title[:80],font=font,fill='#e5e7eb')
            for col,photo in enumerate(item['photos']):
                with Image.open(local_path(root,photo['img'])) as im:
                    tile=ImageOps.contain(im.convert('RGB'),(312,176))
                x=col*320+4;sheet.paste(tile,(x,y+25));draw.text((x,y+200),f'{item["key"]}-{col+1}',font=font,fill='white')
                ids.append(f'bg-{item["id"]}-{col+1}')
        rel=f'review/page-{page_no:02}.jpg';sheet.save(root/rel,quality=90)
        pages.append({'page_id':page_no,'path':str(root/rel),'post_keys':[i['key'] for i in rows],'asset_ids':ids})
    write(catalog/'review-index.json',{'pages':pages,'review_is_complete':False})
    print(json.dumps({'posts':len(posts),'images':len(assets),'unique_hashes':len(groups),'pages':len(pages),'catalog':str(catalog)},ensure_ascii=False))

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    for name in ('build','finalize','check','search','select','record-use','rebind-review'):
        b=sub.add_parser(name);b.add_argument('--root',required=True)
        if name=='check':b.add_argument('--full',action='store_true')
        if name=='search':
            b.add_argument('--brief',required=True);b.add_argument('--limit',type=int,default=8);b.add_argument('--out',required=True)
        if name in ('select','record-use'):b.add_argument('--selection',required=True)
        if name=='select':
            b.add_argument('--asset',required=True);b.add_argument('--reason',required=True);b.add_argument('--viewed',nargs='+',required=True);b.add_argument('--transforms')
        if name=='record-use':
            b.add_argument('--project',required=True);b.add_argument('--variant',required=True);b.add_argument('--status',choices=['used','approved'],default='used')
        if name=='rebind-review':
            b.add_argument('--assets',nargs='+',required=True);b.add_argument('--reason',required=True)
    a=p.parse_args()
    if a.command=='build':build(a.root);return
    import background_selection as bs
    root=Path(a.root).resolve()
    if a.command=='finalize':
        result=bs.finalize(root)
        shutil.copy2(Path(__file__).parent.parent/'assets/background-gallery.html',root/'index.html')
    elif a.command=='check':result=bs.integrity(root,a.full)
    elif a.command=='search':
        if not 1<=a.limit<=40:raise ValueError('limit must be 1..40')
        result=bs.search(root,read(a.brief),a.limit);write(a.out,result)
        result={'action':result['action'],'candidate_count':len(result['candidates']),'selection':a.out,'proposed_asset':result['proposed_asset']}
    elif a.command=='select':result=bs.select(root,a.selection,a.asset,a.reason,a.viewed,read(a.transforms) if a.transforms else None)
    elif a.command=='record-use':result=bs.record_use(root,a.selection,a.project,a.variant,a.status)
    elif a.command=='rebind-review':
        inv=read(root/'catalog/inventory.json');e=root/'review/notes-evidence.json';evidence=read(e) if e.exists() else {};assets={x['id']:x for x in inv['assets']}
        for asset_id in a.assets:
            asset=assets[asset_id]
            if digest(asset['absolute_path'])!=asset['sha256']:raise ValueError('Rebuild inventory first')
            evidence[asset_id]=asset['sha256']
        write(e,evidence)
        result={'at':now(),'assets':a.assets,'reason':a.reason,'attestation':'Assistant explicitly re-opened changed images and updated notes'}
        with (root/'review/reviews.jsonl').open('a') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
