"""
Mark Dirty on Update - POS Sessions Signals

Automatically marks POS session records as dirty when they are updated,
ensuring all changes are tracked for syncing to cloud backend
"""

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db.models import Sum, Q
from decimal import Decimal
from pos_sessions.models import Session, Order, OrderItem

ACTIVE_ORDER_STATUSES = ['New', 'Preparing', 'Ready', 'Completed']


@receiver(post_save, sender=Session)
def mark_session_dirty_on_update(sender, instance, created, **kwargs):
    """Mark Session dirty on update"""
    if not created and hasattr(instance, 'is_dirty') and instance.is_dirty is False:
        instance.is_dirty = True
        instance.save(update_fields=['is_dirty'])


@receiver(post_save, sender=Order)
def update_session_totals_on_order(sender, instance, created, **kwargs):
    """Update session totals when an order is created or updated"""
    if instance.session:
        session = instance.session
        
        # Calculate totals from active orders in this session.
        orders = Order.objects.filter(
            session=session,
            status__in=ACTIVE_ORDER_STATUSES,
        )
        
        # total_sales is the net sales/subtotal figure shown in session summaries.
        total_sales = orders.aggregate(Sum('subtotal'))['subtotal__sum'] or Decimal('0')
        
        # Calculate sales by payment method
        total_cash_sales = orders.filter(payment_method='Cash').aggregate(Sum('total'))['total__sum'] or Decimal('0')
        total_card_sales = orders.filter(payment_method='Card').aggregate(Sum('total'))['total__sum'] or Decimal('0')
        total_mobile_money_sales = orders.filter(payment_method='Mobile Money').aggregate(Sum('total'))['total__sum'] or Decimal('0')
        total_on_account_sales = orders.filter(payment_method='On Account').aggregate(Sum('total'))['total__sum'] or Decimal('0')
        total_other_sales = orders.filter(payment_method='Other').aggregate(Sum('total'))['total__sum'] or Decimal('0')
        
        # Preserve the session tip aggregate pushed from the frontend. The backend
        # Order model no longer stores a per-order tip field, so recomputing tips
        # here would incorrectly zero them out.
        total_tips = session.total_tips or Decimal('0')
        
        # Update session with new totals
        session.total_sales = total_sales
        session.total_cash_sales = total_cash_sales
        session.total_card_sales = total_card_sales
        session.total_mobile_money_sales = total_mobile_money_sales
        session.total_on_account_sales = total_on_account_sales
        session.total_other_sales = total_other_sales
        session.total_tips = total_tips
        
        # Update expected cash (opening float + cash sales)
        session.expected_cash = session.opening_float + total_cash_sales
        
        # Save without triggering the dirty mark signal
        session.save(update_fields=[
            'total_sales',
            'total_cash_sales',
            'total_card_sales',
            'total_mobile_money_sales',
            'total_on_account_sales',
            'total_other_sales',
            'total_tips',
            'expected_cash',
        ])
    
    # Mark order as dirty if it's an update
    if not created and hasattr(instance, 'is_dirty') and instance.is_dirty is False:
        instance.is_dirty = True
        instance.save(update_fields=['is_dirty'])


@receiver(post_save, sender=OrderItem)
def mark_orderitem_dirty_on_update(sender, instance, created, **kwargs):
    """Mark OrderItem dirty on update"""
    if not created and hasattr(instance, 'is_dirty') and instance.is_dirty is False:
        instance.is_dirty = True
        instance.save(update_fields=['is_dirty'])
