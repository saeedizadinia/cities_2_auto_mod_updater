import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import updater as u
class TestUpdater(unittest.TestCase):
    def test_folder(self):
        self.assertEqual(u.parse_folder('move-it_37'),('move-it',37))
        self.assertEqual(u.parse_folder('Move It_40'),('move-it',40))
        self.assertIsNone(u.parse_folder('move-it'))
    def test_slug(self):
        self.assertEqual(u.slug('Find It!'),'find-it')
    def test_download_url(self):
        self.assertEqual(u.make_download_url('cities_skylines_2','move-it',37),
          'https://modscontent.paradox-interactive.com/cities_skylines_2/move-it/content/sources/move-it_37.zip')
    def test_api_contract(self):
        class Resp:
            def raise_for_status(self):pass
            def json(self):return {'modDetail': {'name':'move-it','modVersion':37,'game':'cities_skylines_2'}}
        class Client:
            def get(self,*a,**k):return Resp()
        self.assertEqual(u.metadata(Client(),74324),('move-it',37,'cities_skylines_2',None))

    def test_extract_zip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / 'mod.zip'
            with zipfile.ZipFile(archive, 'w') as zf:
                zf.writestr('wrapped/file.txt', 'works')
            staged = u.extract(archive, None, root / 'out')
            self.assertEqual(staged.name, 'wrapped')
            self.assertEqual((staged / 'file.txt').read_text(), 'works')

    def test_extract_zip_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as zf:
                zf.writestr('../escape.txt', 'bad')
            with self.assertRaisesRegex(u.UpdaterError, 'Unsafe archive entry'):
                u.extract(archive, None, root / 'out')
            self.assertFalse((root / 'escape.txt').exists())

    def test_find_mod_mapping_exact_compact_name(self):
        class Resp:
            def raise_for_status(self): pass
            def json(self):
                return {'mods': [
                    {'game':'cities_skylines_2','state':'published','os':'Windows',
                     'modId':'80529','displayName':'ExtraAssetsImporter'}
                ]}
        class Client:
            def post(self, *args, **kwargs): return Resp()
        match, suggestions = u.find_mod_mapping(Client(), 'extraassetsimporter', Path('.'))
        self.assertEqual(match['modId'], '80529')
        self.assertEqual(suggestions, [])

    def test_find_mod_mapping_rejects_duplicate_names(self):
        class Resp:
            def raise_for_status(self): pass
            def json(self):
                return {'mods': [
                    {'game':'cities_skylines_2','state':'published','os':'Windows',
                     'modId':'1','displayName':'Same Name'},
                    {'game':'cities_skylines_2','state':'published','os':'Any',
                     'modId':'2','displayName':'Same Name'},
                ]}
        class Client:
            def post(self, *args, **kwargs): return Resp()
        match, suggestions = u.find_mod_mapping(Client(), 'same-name', Path('.'))
        self.assertIsNone(match)
        self.assertEqual({x['modId'] for x in suggestions}, {'1', '2'})
if __name__ == '__main__':unittest.main()
