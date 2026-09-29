from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime
from decimal import Decimal
from typing import Mapping,Iterable
from .models import Decision
from .timeutil import utc

@dataclass(frozen=True)
class StockItem:
    sku:str
    quantity:Decimal
    reorder_point:Decimal
    max_quantity:Decimal
    lot_controlled:bool=False

@dataclass(frozen=True)
class Lot:
    lot_id:str
    sku:str
    quantity:Decimal
    expires_at:datetime|None=None
    quarantined:bool=False

@dataclass(frozen=True)
class StockMovement:
    movement_id:str
    sku:str
    quantity:Decimal
    at:datetime
    reason:str
    lot_id:str|None=None
    reference:str=""

class Inventory:
    def __init__(self,items:Iterable[StockItem],lots:Iterable[Lot]=()):
        self.items={i.sku:i for i in items}
        self.lots={l.lot_id:l for l in lots}
        self.movements:list[StockMovement]=[]
        self._ids=set()

    def available(self,sku:str,at:datetime|None=None)->Decimal:
        item=self.items[sku]
        if not item.lot_controlled:return item.quantity
        t=utc(at) if at else None
        total=Decimal("0")
        for lot in self.lots.values():
            if lot.sku!=sku or lot.quarantined:continue
            if t is not None and lot.expires_at is not None and utc(lot.expires_at)<=t:continue
            total+=lot.quantity
        return total

    def issue(self,movement:StockMovement)->Decision:
        if movement.movement_id in self._ids:return Decision(False,"duplicate","stock movement already applied")
        if movement.quantity<=0:return Decision(False,"quantity","issue quantity must be positive")
        at=utc(movement.at)
        item=self.items[movement.sku]
        if item.lot_controlled:
            if not movement.lot_id or movement.lot_id not in self.lots:return Decision(False,"lot","lot required")
            lot=self.lots[movement.lot_id]
            if lot.sku!=movement.sku or lot.quarantined:return Decision(False,"lot","lot unavailable")
            if lot.expires_at is not None and utc(lot.expires_at)<=at:return Decision(False,"expiry","lot expired")
            if lot.quantity<movement.quantity:return Decision(False,"stock","lot quantity insufficient")
            self.lots[movement.lot_id]=replace(lot,quantity=lot.quantity-movement.quantity)
        else:
            if item.quantity<movement.quantity:return Decision(False,"stock","inventory insufficient")
            self.items[item.sku]=replace(item,quantity=item.quantity-movement.quantity)
        self._ids.add(movement.movement_id);self.movements.append(movement)
        return Decision(True,"issued","stock issued",{"remaining":str(self.available(movement.sku,at))})

    def receive(self,movement:StockMovement,lot_expiry:datetime|None=None)->Decision:
        if movement.movement_id in self._ids:return Decision(False,"duplicate","stock movement already applied")
        if movement.quantity<=0:return Decision(False,"quantity","receipt quantity must be positive")
        item=self.items[movement.sku]
        if item.lot_controlled:
            if not movement.lot_id:return Decision(False,"lot","lot required")
            old=self.lots.get(movement.lot_id)
            if old and old.sku!=movement.sku:return Decision(False,"lot","lot SKU mismatch")
            qty=(old.quantity if old else Decimal("0"))+movement.quantity
            self.lots[movement.lot_id]=Lot(movement.lot_id,movement.sku,qty,lot_expiry if old is None else old.expires_at,False if old is None else old.quarantined)
        else:
            qty=item.quantity+movement.quantity
            if qty>item.max_quantity:return Decision(False,"capacity","receipt exceeds inventory capacity")
            self.items[item.sku]=replace(item,quantity=qty)
        self._ids.add(movement.movement_id);self.movements.append(movement)
        return Decision(True,"received","stock received",{"available":str(self.available(movement.sku,utc(movement.at)))})

    def reorder(self,at:datetime)->Mapping[str,Decimal]:
        t=utc(at);out={}
        for sku,item in self.items.items():
            have=self.available(sku,t)
            if have<=item.reorder_point:out[sku]=max(Decimal("0"),item.max_quantity-have)
        return out
