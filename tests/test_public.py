import unittest
from datetime import datetime, timezone, timedelta, time
from decimal import Decimal

from rampflow.timeutil import Window
from rampflow.event_stream import EventStream
from rampflow.ops_store import VersionedStore, VersionConflict
from rampflow.operational_snapshot import SnapshotCodec
from rampflow.fuel_accounting import FuelTank, FuelLedger
from rampflow.crew_duty_control import CrewMember, CrewDutyController, DutySegment
from rampflow.operational_policy import PolicyRule, PolicyContext, PolicyEngine, Effect
from rampflow.service_windows import ServiceWindowRule, ServiceWindowPlanner
from rampflow.resource_dispatch import TravelGraph, MobileResource, ResourceDispatcher, DispatchRequest
from rampflow.stand_orchestration import StandProfile, StandDemand, StandOrchestrator

UTC=timezone.utc
D=lambda h,m=0: datetime(2026,1,1,h,m,tzinfo=UTC)

class PublicTests(unittest.TestCase):
    def test_event_stream_idempotent_publish(self):
        s=EventStream()
        a=s.publish('e1',D(10),'ops','F1',{'x':1})
        b=s.publish('e1',D(10),'ops','F1',{'x':1})
        self.assertEqual(a,b)
        self.assertTrue(s.verify_chain())

    def test_versioned_store_compare_and_swap(self):
        store=VersionedStore()
        rec=store.create('x',{'v':1},'a','create',D(10))
        newer=store.put('x',{'v':2},rec.version,'a','change',D(11))
        self.assertEqual(newer.version,2)
        with self.assertRaises(VersionConflict):
            store.put('x',{'v':3},1,'a','stale',D(12))

    def test_snapshot_round_trip(self):
        codec=SnapshotCodec()
        snap=codec.create('s',2,{'b':2,'a':1},D(10))
        self.assertEqual(codec.decode(codec.encode(snap)).digest,snap.digest)

    def test_fuel_delivery_reduces_inventory(self):
        ledger=FuelLedger([FuelTank('T1',Decimal('10000'),Decimal('5000'),Decimal('0.8'))])
        d=ledger.record('D1','F1','T1',Decimal('10'),Decimal('1010'),Decimal('15'),Decimal('0.8'),Decimal('1.2'),D(10))
        self.assertEqual(d.corrected_l,Decimal('1000.000'))
        self.assertEqual(ledger.inventory_l['T1'],Decimal('4000.000'))

    def test_crew_qualification_enforced(self):
        c=CrewDutyController([CrewMember('C1',frozenset({'push'}),'A',600,600,240)])
        ok,_=c.can_accept('C1',DutySegment('s',Window(D(10),D(11)),'A','push'))
        bad,_=c.can_accept('C1',DutySegment('s2',Window(D(11),D(12)),'A','fuel'))
        self.assertTrue(ok);self.assertFalse(bad)

    def test_policy_requirement(self):
        r=PolicyRule('R',10,frozenset({'push'}),Effect.REQUIRE,'approval required')
        e=PolicyEngine([r])
        ctx=PolicyContext('push','F','APT','T1',D(10),{})
        self.assertFalse(e.evaluate(ctx).allowed)
        ctx2=PolicyContext('push','F','APT','T1',D(10),{'requirement:R':True})
        self.assertTrue(e.evaluate(ctx2).allowed)

    def test_service_window_dependency(self):
        p=ServiceWindowPlanner([
            ServiceWindowRule('clean',0,60,10),
            ServiceWindowRule('board',0,90,20,prerequisites=frozenset({'clean'})),
        ])
        plan=p.plan('F',D(10),D(12),{})
        self.assertLessEqual(plan.bookings['clean'].window.end,plan.bookings['board'].window.start)

    def test_dispatch_shortest_path(self):
        g=TravelGraph();g.add('A','B',100);g.add('B','C',100);g.add('A','C',500)
        d=ResourceDispatcher([MobileResource('R','bus',frozenset(),'A',D(9),10)],g)
        req=DispatchRequest('Q','bus',None,'C',D(10),5,D(11))
        plan=d.plan(req)
        self.assertEqual((plan.arrive_at-plan.depart_at).total_seconds(),20)

    def test_stand_capacity(self):
        o=StandOrchestrator([StandProfile('S','T',40,True,1)])
        a=o.plan(StandDemand('F',Window(D(10),D(11)),35,False))
        o.commit(a)
        with self.assertRaises(Exception):
            o.plan(StandDemand('G',Window(D(10,30),D(11,30)),35,False))

if __name__=='__main__': unittest.main()
