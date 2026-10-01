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
from rampflow.service_windows import ServiceWindowRule, ServiceWindowPlanner, ServiceWindowError
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

    def test_mutual_exclusion_serializes_same_plan_on_one_device(self):
        p=ServiceWindowPlanner([
            ServiceWindowRule('ga',0,120,30,mutual_exclusion_group='hot'),
            ServiceWindowRule('gb',0,120,30,mutual_exclusion_group='hot'),
        ])
        plan=p.plan('F',D(10),D(12),{'ga':('DEV1',),'gb':('DEV1',)})
        a,b=plan.bookings['ga'].window,plan.bookings['gb'].window
        self.assertFalse(a.overlaps(b))
        p.commit(plan)
        self.assertEqual(len(p.resource_bookings['DEV1']),2)

    def test_mutual_exclusion_covers_distinct_devices(self):
        p=ServiceWindowPlanner([
            ServiceWindowRule('ga',0,75,30,mutual_exclusion_group='hot'),
            ServiceWindowRule('gb',0,75,60,mutual_exclusion_group='hot'),
        ])
        with self.assertRaises(ServiceWindowError):
            # Overlapping excluded services cannot fit serially in the window,
            # even when offered two different devices.
            p.plan('F',D(10),D(12),{'ga':('DEV1',),'gb':('DEV2',)})

    def test_stale_plan_rejected_when_resource_generation_moves(self):
        p=ServiceWindowPlanner([
            ServiceWindowRule('ga',0,120,30,mutual_exclusion_group='hot'),
            ServiceWindowRule('gb',0,120,30,mutual_exclusion_group='hot'),
        ])
        first=p.plan('F1',D(10),D(12),{'ga':('DEV1',)})
        held=p.plan('F2',D(10),D(12),{'gb':('DEV1',)})
        p.commit(first)
        with self.assertRaises(ServiceWindowError):
            p.commit(held)
        current=p.plan('F2',D(10),D(12),{'gb':('DEV1',)})
        self.assertGreaterEqual(
            current.bookings['gb'].window.start,
            first.bookings['ga'].window.end,
        )
        p.commit(current)

    def test_release_invalidates_plans_planned_before_it(self):
        p=ServiceWindowPlanner([ServiceWindowRule('a',0,120,30)])
        p.commit(p.plan('F1',D(10),D(12),{'a':('DEV1',)}))
        held=p.plan('F2',D(10),D(12),{'a':('DEV1',)})
        p.release('F1')
        with self.assertRaises(ServiceWindowError):
            p.commit(held)

    def test_rejected_plan_reserves_nothing(self):
        from dataclasses import replace
        p=ServiceWindowPlanner([
            ServiceWindowRule('a',0,60,30),
            ServiceWindowRule('b',0,60,30,prerequisites=frozenset({'a'})),
        ])
        plan=p.plan('F',D(10),D(12),{'a':('DEV1',),'b':('DEV1',)})
        tampered=replace(
            plan,
            bookings={
                **plan.bookings,
                'b':replace(plan.bookings['b'],window=Window(D(10,5),D(10,35))),
            },
        )
        with self.assertRaises(ServiceWindowError):
            p.commit(tampered)
        self.assertEqual(p.committed,{})
        self.assertEqual(p.resource_bookings,{})

    def test_delay_revision_is_validated_and_old_plan_goes_stale(self):
        p=ServiceWindowPlanner([
            ServiceWindowRule('clean',0,120,10),
            ServiceWindowRule('board',0,120,20,prerequisites=frozenset({'clean'})),
        ])
        original=p.plan('F',D(10),D(12),{})
        p.commit(original)
        p.commit(p.delay('F',15))
        self.assertEqual(p.committed['F'].generation,2)
        with self.assertRaises(ServiceWindowError):
            p.commit(original)
        p2=ServiceWindowPlanner([ServiceWindowRule('a',0,30,20)])
        p2.commit(p2.plan('F',D(10),D(12),{}))
        with self.assertRaises(ServiceWindowError):
            p2.delay('F',200)
        self.assertEqual(p2.generation,1)

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
