"""Bounded decompression, source integrity and privacy fallback; synthetic only."""
import hashlib,json,os
from pathlib import Path
import tempfile,unittest
from unittest import mock
from tests.test_training_history import HistoryImport,encoded
import nous_training_history_read as reader
import nous_training_history as history
import nous_training as training

class Fixture(unittest.TestCase):
    setUp=HistoryImport.setUp
    rows=HistoryImport.rows
    source=HistoryImport.source
    plan=HistoryImport.plan
    run_import=HistoryImport.run_import
    records=HistoryImport.records

del HistoryImport

class HistoryRead(Fixture):
    def candidate(self,path,compression='none'):
        return {'path':str(path),'source_id':hashlib.sha256(os.fsencode(str(path))).hexdigest(),
                'stat':reader._signature(path.stat()),'compression':compression}
    def test_plain_and_zstd_bind_different_raw_same_expanded(self):
        if reader._zstd is None:self.skipTest('stdlib zstd unavailable')
        plain=self.source();raw=plain.read_bytes();packed=plain.with_suffix('.jsonl.zst')
        packed.write_bytes(reader._zstd.compress(raw))
        a=reader.read_history_bytes(self.candidate(plain));b=reader.read_history_bytes(self.candidate(packed,'zstd'))
        self.assertEqual(a['snapshot'],b['snapshot']);self.assertEqual(a['snapshot_sha256'],b['snapshot_sha256'])
        self.assertNotEqual(a['source_sha256'],b['source_sha256'])
        before=packed.read_bytes();report,_=self.run_import()
        self.assertEqual(report['counts'],{'imported':2});self.assertEqual(before,packed.read_bytes())
    def test_zstd_truncated_corrupt_and_concatenated_frames(self):
        if reader._zstd is None:self.skipTest('stdlib zstd unavailable')
        p=self.root/'codex'/'packed.jsonl.zst';a,b=b'{"a":1}\n',b'{"b":2}\n'
        p.write_bytes(reader._zstd.compress(a)+reader._zstd.compress(b))
        self.assertEqual(reader.read_history_bytes(self.candidate(p,'zstd'))['snapshot'],a+b)
        for data in (p.read_bytes()[:-1],b'invalid-zstd',p.read_bytes()+b'garbage'):
            p.write_bytes(data)
            with self.assertRaisesRegex(reader.HistoryReadError,'history_compression_invalid'):
                reader.read_history_bytes(self.candidate(p,'zstd'))
    def test_zstd_decompressed_limit_and_frame_budget(self):
        if reader._zstd is None:self.skipTest('stdlib zstd unavailable')
        p=self.root/'codex'/'packed.jsonl.zst';p.write_bytes(reader._zstd.compress(b'a'*1000))
        with mock.patch.object(reader,'MAX_BYTES',100),self.assertRaisesRegex(reader.HistoryReadError,'transcript_too_large'):
            reader.read_history_bytes(self.candidate(p,'zstd'))
        p.write_bytes(reader._zstd.compress(b'a')+reader._zstd.compress(b'b'))
        with mock.patch.object(reader,'MAX_FRAMES',1),self.assertRaisesRegex(reader.HistoryReadError,'history_frame_limit'):
            reader.read_history_bytes(self.candidate(p,'zstd'))
    def test_no_zstd_capability_does_not_claim_empty_history(self):
        p=self.root/'codex'/'packed.jsonl.zst';p.write_bytes(b'fixture')
        with mock.patch.object(reader,'_zstd',None):
            plan=self.plan()
            self.assertEqual(plan['scan_skipped_counts'],{'scan_compressed_unsupported':1})
    def test_header_limit_does_not_validate_truncated_tail(self):
        if reader._zstd is None:self.skipTest('stdlib zstd unavailable')
        p=self.root/'codex'/'packed.jsonl.zst';p.write_bytes(reader._zstd.compress(encoded(self.rows('codex'))+b'x'*200000)[:-1])
        c=self.candidate(p,'zstd');header=reader.read_history_header(str(p),c['stat'],'zstd',max_lines=1)
        self.assertFalse(header['header']['complete'])
        with self.assertRaises(reader.HistoryReadError):reader.read_history_bytes(c)
    def test_fifo_and_symlink_not_read(self):
        p=self.root/'codex'/'fifo.jsonl';os.mkfifo(p)
        with self.assertRaisesRegex(reader.HistoryReadError,'history_regular_file_required'):
            reader.read_history_bytes(self.candidate(p))
        real=self.source();link=self.root/'codex'/'link.jsonl';link.symlink_to(real)
        with self.assertRaises(reader.HistoryReadError):reader.read_history_bytes(self.candidate(link))

class HistoryOffline(Fixture):
    def test_unavailable_workspace_tool_payloads_removed_before_scrub_without_operand_io(self):
        missing=self.root/'old-project'
        rows=self.rows(project=missing)
        rows.insert(1,{'type':'assistant','sessionId':'synthetic-session','cwd':str(missing),'uuid':'toolcall','parentUuid':'user-id',
            'message':{'role':'assistant','content':[{'type':'tool_use','id':'read-1','name':'Read','input':{'file_path':'/private/secret.env','password':'tiny-secret'}}]}})
        rows.insert(2,{'type':'user','sessionId':'synthetic-session','cwd':str(missing),'uuid':'toolresult','parentUuid':'toolcall',
            'message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'read-1','content':'tiny-result','is_error':True}]}})
        rows[-1]['parentUuid']='toolresult';self.source(rows=rows)
        original=training.scrub_record;inspected=[]
        def inspect(value,*args,**kwargs):
            raw=json.dumps(value)
            self.assertNotIn('tiny-secret',raw);self.assertNotIn('tiny-result',raw);self.assertNotIn('/private/secret.env',raw)
            inspected.append(True);return original(value,*args,**kwargs)
        import nous_privacy
        with mock.patch.object(training,'scrub_record',side_effect=inspect),mock.patch.object(nous_privacy,'excluded',side_effect=AssertionError('must not inspect old operands')):
            report,_=self.run_import()
        self.assertTrue(inspected);self.assertEqual(report['counts'],{'imported':1})
        self.assertEqual(report['tool_payloads_omitted'],2);self.assertFalse(missing.exists())
        record=next(iter(self.records().values()));self.assertFalse(record['quality']['complete']);self.assertEqual(record['evidence'],[])
        result=[b for m in record['messages'] for b in m['content'] if b['type']=='tool_result'][0]
        self.assertTrue(result['is_error']);self.assertEqual(report['datasets']['trl']['counts']['sft'],0)

    def test_offline_codemode_stays_incomplete_visible_projection(self):
        from tests.test_training_history import HOOKS
        rows=[json.loads(line) for line in (HOOKS.parent/'examples/training/codemode.jsonl').read_text().splitlines()]
        missing=self.root/'missing';rows[0]['payload'].update(id='legacy-offline',cwd=str(missing),source='cli')
        self.source(agent='codex',rows=rows)
        report,_=self.run_import();value=next(iter(self.records().values()))
        self.assertEqual(value['schema'],'nous.trajectory/1');self.assertNotIn('protocol',value)
        self.assertIn('historical_native_protocol_unavailable',value['quality']['issues'])
        self.assertEqual(report['datasets']['trl']['counts']['sft'],0)
    def test_hidden_reasoning_is_not_recovered_as_visible_history(self):
        rows=self.rows(project=self.root/'missing')
        rows[-1]['message']['content']=[{'type':'thinking','thinking':'HIDDEN_SECRET_TEST'},{'type':'text','text':'visible answer'}]
        self.source(rows=rows);self.run_import()
        self.assertNotIn('HIDDEN_SECRET_TEST',json.dumps(self.records()))

if __name__=='__main__':unittest.main()
