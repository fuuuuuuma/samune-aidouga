import copy, json, tempfile, unittest
from pathlib import Path
from background_library import color_name, build, read, write, digest, ZONES
from background_selection import rank_assets, integrity, finalize, search, select
from PIL import Image


def asset(id='one',sha='hash',left=20,right=235,**kw):
    cells=[]
    for r in range(3):
        for c in range(3):cells.append({'bbox':[c/3,r/3,(c+1)/3,(r+1)/3],'zone':ZONES[r*3+c],'luma':left if c==0 else right,'quiet_score':90,'highlight_fraction':0,'edge':2})
    return dict(id=id,sha256=sha,absolute_path='/fixture/'+id,image_path='images/'+id,thumbnail_path='thumbs/'+id,post_key='P001',visual_status='reviewed',roles=['base','overlay'],tags=['デジタル'],visual_description='青いデジタル',description='青いデジタル',color_names=['青'],category='light',lighting='暗め',density='低密度',width=1456,height=816,aspect_ratio=1.78,grid=cells,native_4k=False,**kw)

class RankingTests(unittest.TestCase):
    def test_white_text_prefers_dark_left_region(self):
        a=asset('leftdark','a',20,230);b=asset('leftlight','b',230,20)
        brief={'text_regions':[{'bbox':[0,0,.33,.8],'text_tone':'light'}]}
        self.assertEqual(rank_assets([b,a],brief)['proposed_asset'],'leftdark')
        brief['text_regions'][0]['text_tone']='dark'
        self.assertEqual(rank_assets([b,a],brief)['proposed_asset'],'leftlight')
    def test_duplicate_hash_collapses_and_used_base_excluded(self):
        a=asset('a');b=asset('b');c=asset('c','other')
        self.assertEqual(len(rank_assets([a,b,c],{})['candidates']),2)
        self.assertEqual(rank_assets([a,b,c],{},['hash'])['proposed_asset'],'c')
        self.assertEqual(len(rank_assets([a,b,c],{'role':'overlay'},['hash'])['candidates']),2)
    def test_revision_background_unchanged_is_preserved(self):
        self.assertEqual(rank_assets([asset()],{'phase':'revision'})['action'],'preserve')
        self.assertEqual(rank_assets([asset()],{'phase':'revision','background_change_requested':True})['action'],'choose')
    def test_provided_screenshot_is_not_replaced(self):
        self.assertEqual(rank_assets([asset()],{'base_mode':'provided_image'})['action'],'use_provided')
        self.assertEqual(rank_assets([asset()],{'base_mode':'provided_scene'})['action'],'use_provided')
        self.assertEqual(rank_assets([asset()],{'base_mode':'none'})['action'],'none')
    def test_unreviewed_or_wrong_scene_not_fallback(self):
        a=asset();a['visual_status']='pending'
        self.assertEqual(rank_assets([a],{})['action'],'no_match')
        self.assertEqual(rank_assets([asset()],{'required_terms':['レモン']})['action'],'no_match')
    def test_fruit_requires_topic_match(self):
        a=asset();a['tags']=['果物','レモン'];a['visual_description']='黄色いレモン'
        self.assertEqual(rank_assets([a],{'query':'高級感'})['action'],'no_match')
        self.assertEqual(rank_assets([a],{'query':'レモン'})['action'],'choose')
    def test_invalid_geometry_rejected(self):
        with self.assertRaises(ValueError):rank_assets([] ,{'text_regions':[{'bbox':[.8,0,.2,1]}]})
    def test_primary_color_names(self):
        self.assertEqual([color_name(c) for c in [(255,0,0),(0,0,255),(255,255,0),(0,255,0),(255,0,180)]],['赤','青','黄','緑','桃'])

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'images/light').mkdir(parents=True)
        Image.new('RGB',(320,180),'navy').save(self.root/'images/light/old.png')
        data={'cats':[{'id':'light','name':'光'}],'items':[{'id':'1','cat':'light','date':'2026-01-01','text':'fixture','photos':[{'img':'images/light/old.png','thumb':'images/light/old.png'}]}]}
        (self.root/'index.html').write_text('<script>const DATA = '+json.dumps(data)+';</script>');build(self.root)
        (self.root/'review/visual-notes.tsv').write_text('P001\t青い光\tbase,overlay\t青,光\tfixture\n')
    def tearDown(self):self.tmp.cleanup()
    def test_missing_review_fails(self):
        (self.root/'review/visual-notes.tsv').write_text('')
        with self.assertRaises(ValueError):finalize(self.root)
    def test_additions_keep_existing_ids_and_originals(self):
        before=digest(self.root/'images/light/old.png');Image.new('RGB',(320,180),'gold').save(self.root/'images/light/new.png')
        self.assertEqual(len(integrity(self.root)['added']),1);build(self.root)
        rows=read(self.root/'catalog/posts.json');self.assertEqual(rows[0]['key'],'P001');self.assertEqual(rows[0]['id'],'1');self.assertEqual(rows[1]['key'],'P002')
        self.assertEqual(digest(self.root/'images/light/old.png'),before)
        self.assertIsNone(read(self.root/'catalog/inventory.json')['assets'][1]['source_url'])
    def test_changed_image_requires_new_review_evidence(self):
        finalize(self.root);Image.new('RGB',(320,180),'red').save(self.root/'images/light/old.png')
        self.assertEqual(len(integrity(self.root,True)['changed']),1)
        with self.assertRaises(ValueError):search(self.root,{})
        build(self.root)
        with self.assertRaises(ValueError):finalize(self.root)
        with self.assertRaises(ValueError):search(self.root,{})
    def test_proposal_cannot_be_selected_without_viewing(self):
        finalize(self.root);r=search(self.root,{});path=self.root/'selection.json';write(path,r);id=r['proposed_asset']
        with self.assertRaises(ValueError):select(self.root,path,id,'reason',[])
        selected=select(self.root,path,id,'青い主題と暗い文字領域の一致',[id])
        self.assertFalse(selected['approved_by_user']);self.assertFalse((self.root/'catalog/usage.jsonl').exists())

if __name__=='__main__':unittest.main()
