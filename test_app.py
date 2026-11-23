import sqlite3
import tempfile
import unittest
from contextlib import closing
from collections import Counter
from pathlib import Path
from app import Bakery, ROOT, START, END, PRODUCTS

class BakeryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'bakery.db'
        # Read the real DB only to copy it. All modifications happen in the copy.
        with closing(sqlite3.connect(f'file:{(ROOT/"bakery.db").as_posix()}?mode=ro',uri=True)) as source, closing(sqlite3.connect(self.path)) as dest:
            source.backup(dest)
        with closing(sqlite3.connect(self.path)) as db:
            for table in ('requests','events','imports','orders','days','meta'):db.execute(f'DELETE FROM {table}')
            db.commit()
        self.app=Bakery(self.path)
    def tearDown(self):self.temp.cleanup()
    def load(self):return self.app.action('fetch',{})
    def close_day(self):return self.app.action('advance',{})
    def request(self,name):return max((r for r in self.app.state()['requests'] if r['customer']==name),key=lambda r:r['id'])
    def order(self,name):return next(o for o in self.app.state()['orders'] if o['customer']==name)
    def until(self,day):
        while self.app.state()['today']<day:self.load();self.close_day()
    def assert_capacity(self):
        for d in self.app.state()['days']:self.assertLessEqual(d['used'],d['limit'])
    def test_initial_and_repeat_import(self):
        s=self.app.state();self.assertEqual(len(s['orders']),13);self.assertEqual(s['value'],196000)
        self.load();self.load();self.assertEqual(len(self.app.state()['requests']),3)
        self.assertFalse(self.request('Okafor')['eligible']);self.assertTrue(self.request('Jasmine')['eligible'])
    def test_manual_accept_and_restart(self):
        self.load();rid=self.request('Jasmine')['id'];self.app.action('accept',{'id':rid})
        self.assertEqual(Bakery(self.path).state()['value'],208000)
        with self.assertRaises(ValueError):self.app.action('accept',{'id':rid})
        self.assertEqual(len(self.app.state()['orders']),14)
    def test_cancel_frees_capacity(self):
        self.until('2026-11-24');self.load()
        self.assertEqual(self.order('Brown')['status'],'Cancelled')
        self.assertTrue(self.request('Reyes')['eligible'])
        self.close_day();self.assertEqual(self.order('Reyes')['minutes'],180);self.assert_capacity()
    def test_change_delta_and_late_change(self):
        self.until('2026-11-25');self.load()
        self.assertEqual(self.order('Moreau')['qty'],6)
        self.assertEqual(next(d for d in self.app.state()['days'] if d['day']=='2026-11-28')['used'],420)
        self.close_day();self.load()
        self.assertEqual(self.order('Lam')['qty'],2)
        self.assertEqual(self.request('Lam')['status'],'Incompatible')
    def test_helper_notice_duplicate_and_capacity(self):
        with self.assertRaises(ValueError):self.app.action('helper',{'day':'2026-11-24'})
        self.app.action('helper',{'day':'2026-11-25'})
        with self.assertRaises(ValueError):self.app.action('helper',{'day':'2026-11-25'})
        self.assertEqual(self.app.state()['costs'],10000)
        self.assertEqual(next(d for d in self.app.state()['days'] if d['day']=='2026-11-25')['limit'],720)
    def test_sequential_batch_and_helper_rescue(self):
        self.until('2026-11-26');self.load()
        self.assertFalse(self.request('Wilson')['eligible'])
        self.app.action('helper',{'day':'2026-11-28'})
        self.assertTrue(self.request('Wilson')['eligible'])
        self.close_day()
        self.assertEqual(self.request('Wilson')['status'],'Accepted')
        self.assertEqual(self.request('Novak')['status'],'Accepted')
        self.assertEqual(self.request('Bell')['status'],'Rejected')
        self.assert_capacity()
    def test_preview_is_read_only(self):
        self.load();before=self.app.state();forecast=self.app.preview();self.assertIn('Accepted 2',forecast['summary']);self.assertEqual(before,self.app.state())
    def test_advance_requires_fetch(self):
        with self.assertRaises(ValueError):self.close_day()
        self.assertEqual(self.app.state()['today'],START.isoformat())
    def test_full_week_without_helpers(self):
        self.until(END.isoformat());s=self.app.state()
        self.assertEqual(len(s['requests']),25)
        self.assertEqual(Counter(r['status'] for r in s['requests']),{'Accepted':13,'Rejected':9,'Applied':3})
        self.assertEqual(len(s['orders']),26)
        self.assertEqual(Counter(o['status'] for o in s['orders']),{'Picked up':24,'Cancelled':2})
        self.assertEqual(self.order('Zhou')['qty'],4)
        self.assertEqual(self.order('Lee')['status'],'Picked up')
        self.assertEqual(s['sales'],364000);self.assertEqual(s['sales'],s['value']);self.assertEqual(s['costs'],0);self.assert_capacity()
        with self.assertRaises(ValueError):self.close_day()
        print('\nNo-helper final value:',s['value']/100)
    def test_full_week_with_helpers(self):
        # Book Fri and Sat while eligible; never exceed 12 h.
        for d in ('2026-11-27','2026-11-28'):self.app.action('helper',{'day':d})
        while self.app.state()['today']<END.isoformat():self.load();self.close_day();self.assert_capacity()
        s=self.app.state();self.assertEqual(s['costs'],20000);self.assertEqual(s['sales'],408000)
        self.assertEqual(self.request('Park')['status'],'Accepted')
        self.assertEqual(self.request('Singh')['status'],'Accepted')
        self.assertEqual(self.request('Wilson')['status'],'Accepted')
        self.assertEqual(self.request('Novak')['status'],'Rejected')
        print('\nWith-helper final value:',s['value']/100)

if __name__=='__main__':unittest.main(verbosity=2)
