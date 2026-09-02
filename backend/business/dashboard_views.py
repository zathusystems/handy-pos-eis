from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from datetime import timedelta, datetime
from django.db.models import Sum, Count, Q
from django.db.utils import OperationalError
from pos_sessions.models import Order, OrderItem, Session
from inventory.models import InventoryItem
from take_orders.models import TakeOrder
from .models import Business, Branch
from decimal import Decimal
import re
import logging

logger = logging.getLogger(__name__)

class DashboardViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def _get_user_business(self, request):
        """Get the user's business (owner or assigned staff business)."""
        business = Business.objects.filter(owner=request.user).first()
        if business:
            return business

        try:
            from staff.models import Staff
            staff = Staff.objects.select_related('business').filter(
                user=request.user,
                is_active=True
            ).first()
            return staff.business if staff and staff.business else None
        except Exception:
            return None

    def _get_active_branch(self, request):
        """Get the active branch for the user"""
        business = self._get_user_business(request)
        if not business:
            return None

        # Try to resolve branch from query params, otherwise fall back to first branch.
        raw_branch_ref = request.query_params.get('branch_id')
        branch_ref = self._normalize_branch_reference(raw_branch_ref)

        if isinstance(branch_ref, int):
            branch = Branch.objects.filter(id=branch_ref, business=business).first()
            if branch:
                return branch
        elif isinstance(branch_ref, str):
            normalized = branch_ref.lower()
            if normalized in {'main', 'main-branch', 'main_branch'}:
                branch = Branch.objects.filter(
                    business=business,
                    name__iendswith='Main Branch'
                ).order_by('created_at', 'id').first()
                if branch:
                    return branch

            branch = (
                Branch.objects.filter(business=business, slug=branch_ref).first()
                or Branch.objects.filter(business=business, name__iexact=branch_ref).first()
            )
            if branch:
                return branch

        return business.branches.first()

    @staticmethod
    def _normalize_branch_reference(raw_reference):
        """
        Normalize branch references from client query params.
        Supports integer IDs, BRN-<id> and string aliases.
        """
        if raw_reference is None:
            return None

        value = str(raw_reference).strip()
        if not value:
            return None

        lowered = value.lower()
        if lowered in {'nan', 'null', 'none', 'undefined'}:
            return None

        legacy_match = re.match(r'^BRN-(\d+)$', value, flags=re.IGNORECASE)
        if legacy_match:
            return int(legacy_match.group(1))

        if value.isdigit():
            return int(value)

        return value

    def _is_admin_user(self, request, business):
        """
        True when user is business owner or staff role is Admin.
        Non-admin users must be scoped to their own active session.
        """
        if not business:
            return False

        if business.owner_id == request.user.id:
            return True

        try:
            from staff.models import Staff, StaffRole
            staff = Staff.objects.filter(
                user=request.user,
                business=business,
                is_active=True,
            ).only('role').first()
            return bool(staff and staff.role == StaffRole.ADMIN)
        except Exception:
            return False

    def _apply_subscription_daily_charges(self, business):
        """
        Best effort: apply pending daily subscription charges on dashboard access.
        Safe to call repeatedly; charging logic is date-bound in subscription model.
        """
        if not business:
            return

        try:
            from subscription.models import Subscription

            subscription = (
                Subscription.objects.filter(business=business)
                .order_by('-id')
                .first()
            )
            if not subscription:
                return

            charged, message, charged_days, charged_amount = subscription.apply_pending_daily_charges()
            if charged:
                logger.info(
                    "[DASHBOARD] Applied pending subscription charges: business=%s, days=%s, amount=%s",
                    business.id,
                    charged_days,
                    charged_amount,
                )
            else:
                logger.debug(
                    "[DASHBOARD] Skipped pending subscription charges: business=%s, reason=%s",
                    business.id,
                    message,
                )
        except OperationalError as exc:
            logger.warning(
                "[DASHBOARD] Skipping subscription billing due to database lock (business=%s): %s",
                business.id,
                exc,
            )
        except Exception as exc:
            logger.warning(
                "[DASHBOARD] Unable to apply subscription charges (business=%s): %s",
                getattr(business, 'id', None),
                exc,
            )

    def _calculate_cogs(self, order_items, inventory_map):
        """Calculate Cost of Goods Sold for order items"""
        total_cogs = Decimal('0.00')
        for item in order_items:
            inv_item = inventory_map.get(item.inventory_item_id)
            if not inv_item:
                continue
            
            cost = inv_item.get('cost') or Decimal('0.00')
            total_cogs += cost * item.quantity
        
        return total_cogs

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Get dashboard summary data"""
        business = self._get_user_business(request)
        if not business:
            return Response(
                {'detail': 'No business found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Keep subscription billing in sync when users open dashboard.
        self._apply_subscription_daily_charges(business)
        
        branch = self._get_active_branch(request)
        if not branch:
            return Response(
                {'detail': 'No branch found'},
                status=status.HTTP_404_NOT_FOUND
            )

        is_admin_user = self._is_admin_user(request, business)
        
        # Get date range from query params
        from_date_str = request.query_params.get('from_date')
        to_date_str = request.query_params.get('to_date')
        
        if from_date_str and to_date_str:
            try:
                from_date = datetime.fromisoformat(from_date_str.replace('Z', '+00:00'))
                to_date = datetime.fromisoformat(to_date_str.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                # Default to last 7 days
                to_date = timezone.now()
                from_date = to_date - timedelta(days=7)
        else:
            # Default to last 7 days
            to_date = timezone.now()
            from_date = to_date - timedelta(days=7)

        # Resolve active session scope first.
        active_session_queryset = Session.objects.select_related('user').filter(
            branch=branch,
            status='active'
        )
        if not is_admin_user:
            active_session_queryset = active_session_queryset.filter(user=request.user)
        active_sessions = list(active_session_queryset.order_by('-started_at'))
        active_session = active_sessions[0] if active_sessions else None

        # Fetch orders for the branch and date range
        orders = Order.objects.filter(
            branch=branch,
            order_type='sale',
            created_at__gte=from_date,
            created_at__lte=to_date
        ).prefetch_related('items')

        # Non-admin users only see data from their own active session in this branch.
        if not is_admin_user:
            if active_session:
                orders = orders.filter(session=active_session)
            else:
                orders = orders.none()
        
        # Fetch inventory for the branch
        inventory_items = InventoryItem.objects.filter(branch=branch)
        inventory_map = {
            str(item.id): {
                'id': str(item.id),
                'name': item.name,
                'cost': item.cost or Decimal('0.00'),
                'price': item.price or Decimal('0.00'),
                'stock_units': item.stock_units,
                'reorder_level': item.reorder_level,
                'status': item.status,
                'value': item.value,
                'expiry': item.expiry,
                'unit_type': item.unit_type,
            }
            for item in inventory_items
        }
        
        # Calculate KPIs
        total_sales = Decimal('0.00')
        total_sales_before_tax = Decimal('0.00')
        total_cogs = Decimal('0.00')
        total_transactions = 0
        payment_totals = {
            'Cash': Decimal('0.00'),
            'Card': Decimal('0.00'),
            'Mobile Money': Decimal('0.00'),
            'On Account': Decimal('0.00'),
            'Other': Decimal('0.00'),
        }
        product_sales = {}
        
        for order in orders:
            total_sales += order.total
            order_subtotal = (
                order.net_amount
                or order.subtotal
                or (order.total - (order.vat_amount or order.tax or Decimal('0.00')))
            )
            total_sales_before_tax += order_subtotal
            total_transactions += 1
            
            # Sum revenue per payment method (not transaction count)
            payment_method = (order.payment_method or '').strip()
            if payment_method in payment_totals:
                payment_totals[payment_method] += order.total
            else:
                payment_totals['Other'] += order.total
            
            # Calculate COGS and track products
            if order.cogs and order.cogs > 0:
                total_cogs += order.cogs

            for item in order.items.all():
                inv_item = inventory_map.get(item.inventory_item_id)
                if not inv_item:
                    continue
                
                cost = inv_item['cost']
                price = inv_item['price']
                
                if not order.cogs or order.cogs <= 0:
                    total_cogs += cost * item.quantity
                
                # Track product sales
                if item.inventory_item_id not in product_sales:
                    product_sales[item.inventory_item_id] = {
                        'name': item.name,
                        'unitsSold': 0,
                        'revenue': Decimal('0.00'),
                        'profit': Decimal('0.00'),
                    }
                
                product_sales[item.inventory_item_id]['unitsSold'] += int(item.quantity)
                product_sales[item.inventory_item_id]['revenue'] += price * item.quantity
                product_sales[item.inventory_item_id]['profit'] += (price - cost) * item.quantity
        
        gross_profit = total_sales_before_tax - total_cogs
        avg_sale_value = total_sales / total_transactions if total_transactions > 0 else Decimal('0.00')
        inventory_value = sum(Decimal(str(item['value'])) for item in inventory_map.values())
        
        # Format KPI data
        kpi_data = [
            {
                'title': 'Total Revenue (with Tax)',
                'value': float(total_sales),
                'change': '',
                'icon': 'DollarSign',
            },
            {
                'title': 'Gross Profit',
                'value': float(gross_profit),
                'change': '',
                'icon': 'TrendingUp',
            },
            {
                'title': 'Total Transactions',
                'value': total_transactions,
                'change': '',
                'icon': 'ShoppingCart',
            },
            {
                'title': 'Inventory Value',
                'value': float(inventory_value),
                'change': '',
                'icon': 'Package',
            },
        ]
        
        # Get top products (limit to 5)
        top_products = sorted(
            product_sales.values(),
            key=lambda x: x['revenue'],
            reverse=True
        )[:5]
        
        # Format payment data
        payment_data = [
            {'name': 'Cash', 'value': float(payment_totals.get('Cash', Decimal('0.00'))), 'color': 'hsl(var(--chart-1))'},
            {'name': 'Card', 'value': float(payment_totals.get('Card', Decimal('0.00'))), 'color': 'hsl(var(--chart-2))'},
            {'name': 'Mobile Money', 'value': float(payment_totals.get('Mobile Money', Decimal('0.00'))), 'color': 'hsl(var(--chart-3))'},
            {'name': 'On Account', 'value': float(payment_totals.get('On Account', Decimal('0.00'))), 'color': 'hsl(var(--chart-4))'},
            {'name': 'Other', 'value': float(payment_totals.get('Other', Decimal('0.00'))), 'color': 'hsl(var(--chart-5))'},
        ]
        
        # Generate sales data by day
        sales_data = []
        current_date = from_date
        while current_date <= to_date:
            day_start = timezone.make_aware(datetime.combine(current_date.date(), datetime.min.time()))
            day_end = timezone.make_aware(datetime.combine(current_date.date(), datetime.max.time()))
            
            day_sales = orders.filter(
                created_at__gte=day_start,
                created_at__lte=day_end
            ).aggregate(total=Sum('total'))['total'] or Decimal('0.00')
            
            sales_data.append({
                'name': current_date.strftime('%b %d'),
                'total': float(day_sales),
            })
            
            current_date += timedelta(days=1)
        
        # Get low stock items (admin-only dashboard insight)
        low_stock_base = inventory_items if is_admin_user else inventory_items.none()
        low_stock_items = low_stock_base.filter(
            item_type='ingredient',
            stock_units__lte=Decimal('0.00')
        ).values(
            'id', 'name', 'category', 'item_type', 'stock_units', 'unit_type',
            'reorder_level', 'status', 'cost', 'price', 'value', 'expiry'
        )[:4]
        
        active_session_data = None
        if active_session:
            active_order_statuses = ['New', 'Preparing', 'Ready', 'Completed']
            # Calculate session totals from active session(s)
            if is_admin_user:
                session_orders = Order.objects.filter(
                    session__in=active_sessions,
                    status__in=active_order_statuses,
                )
            else:
                session_orders = Order.objects.filter(
                    session=active_session,
                    status__in=active_order_statuses,
                )
            
            total_sales = Decimal('0.00')
            total_cash_sales = Decimal('0.00')
            total_card_sales = Decimal('0.00')
            total_mobile_money_sales = Decimal('0.00')
            total_on_account_sales = Decimal('0.00')
            total_other_sales = Decimal('0.00')
            
            for order in session_orders:
                total_sales += order.subtotal
                
                # Normalize payment method (strip whitespace, handle case sensitivity)
                pm = str(order.payment_method).strip() if order.payment_method else 'Cash'
                
                if pm == 'Cash':
                    total_cash_sales += order.total
                elif pm == 'Card':
                    total_card_sales += order.total
                elif pm == 'Mobile Money':
                    total_mobile_money_sales += order.total
                elif pm == 'On Account':
                    total_on_account_sales += order.total
                else:
                    total_other_sales += order.total
            
            if is_admin_user:
                # Use stored session totals if orders are not yet synced.
                if total_sales == 0:
                    total_sales = sum((s.total_sales for s in active_sessions), Decimal('0.00'))
                    total_cash_sales = sum((s.total_cash_sales for s in active_sessions), Decimal('0.00'))
                    total_card_sales = sum((s.total_card_sales for s in active_sessions), Decimal('0.00'))
                    total_mobile_money_sales = sum(
                        (s.total_mobile_money_sales for s in active_sessions),
                        Decimal('0.00')
                    )
                    total_on_account_sales = sum((s.total_on_account_sales for s in active_sessions), Decimal('0.00'))
                    total_other_sales = sum((s.total_other_sales for s in active_sessions), Decimal('0.00'))

                opening_float_total = sum((s.opening_float for s in active_sessions), Decimal('0.00'))
                expected_cash = opening_float_total + total_cash_sales
                total_tips = sum((s.total_tips for s in active_sessions), Decimal('0.00'))
                earliest_started_at = min((s.started_at for s in active_sessions))
                active_session_count = len(active_sessions)

                active_session_data = {
                    'id': str(active_session.id) if active_session_count == 1 else 'multiple',
                    'status': 'active',
                    'started_by_user_id': None,
                    'started_by_name': (
                        '1 active session in this branch'
                        if active_session_count == 1
                        else f'{active_session_count} active sessions in this branch'
                    ),
                    'started_by_email': None,
                    'opening_float': float(opening_float_total),
                    'expected_cash': float(expected_cash),
                    'actual_cash': None,
                    'closing_float': None,
                    'difference': None,
                    'total_sales': float(total_sales),
                    'total_cash_sales': float(total_cash_sales),
                    'total_card_sales': float(total_card_sales),
                    'total_mobile_money_sales': float(total_mobile_money_sales),
                    'total_on_account_sales': float(total_on_account_sales),
                    'total_tips': float(total_tips),
                    'started_at': earliest_started_at.isoformat(),
                    'closed_at': None,
                    'active_session_count': active_session_count,
                }
            else:
                # Use stored session totals if no orders found (they may not be synced yet)
                # This ensures the dashboard shows the correct totals even before sync
                if total_sales == 0 and active_session.total_sales > 0:
                    total_sales = active_session.total_sales
                    total_cash_sales = active_session.total_cash_sales
                    total_card_sales = active_session.total_card_sales
                    total_mobile_money_sales = active_session.total_mobile_money_sales
                    total_on_account_sales = active_session.total_on_account_sales
                    total_other_sales = active_session.total_other_sales or Decimal('0.00')

                # Calculate expected cash
                expected_cash = active_session.opening_float + total_cash_sales

                started_by_name = None
                if getattr(active_session, 'user', None):
                    first_name = (getattr(active_session.user, 'first_name', '') or '').strip()
                    last_name = (getattr(active_session.user, 'last_name', '') or '').strip()
                    full_name = f"{first_name} {last_name}".strip()
                    started_by_name = (
                        full_name
                        or first_name
                        or (getattr(active_session.user, 'email', '') or '').strip()
                        or (getattr(active_session.user, 'phone', '') or '').strip()
                        or (getattr(active_session.user, 'username', '') or '').strip()
                        or str(active_session.user_id)
                    )

                active_session_data = {
                    'id': str(active_session.id),
                    'status': active_session.status,
                    'started_by_user_id': str(active_session.user_id) if active_session.user_id else None,
                    'started_by_name': started_by_name,
                    'started_by_email': active_session.user.email if getattr(active_session, 'user', None) else None,
                    'opening_float': float(active_session.opening_float),
                    'expected_cash': float(expected_cash),
                    'actual_cash': float(active_session.actual_cash) if active_session.actual_cash else None,
                    'closing_float': float(active_session.closing_float) if active_session.closing_float else None,
                    'difference': float(active_session.difference) if active_session.difference else None,
                    'total_sales': float(total_sales),
                    'total_cash_sales': float(total_cash_sales),
                    'total_card_sales': float(total_card_sales),
                    'total_mobile_money_sales': float(total_mobile_money_sales),
                    'total_on_account_sales': float(total_on_account_sales),
                    'total_tips': float(active_session.total_tips),
                    'started_at': active_session.started_at.isoformat(),
                    'closed_at': active_session.closed_at.isoformat() if active_session.closed_at else None,
                    'active_session_count': 1,
                }
        
        # Format low stock items
        low_stock_items_data = [
            {
                'id': str(item['id']),
                'name': item['name'],
                'category': item['category'],
                'item_type': item['item_type'],
                'stock_units': float(item['stock_units']),
                'unit_type': item['unit_type'],
                'reorder_level': float(item['reorder_level']),
                'status': item['status'],
                'cost': float(item['cost']) if item['cost'] else None,
                'price': float(item['price']) if item['price'] else None,
                'value': float(item['value']),
                'expiry': item['expiry'].isoformat() if item['expiry'] else None,
            }
            for item in low_stock_items
        ]
        
        # Get today's take orders (limit to 5)
        today_start = timezone.make_aware(datetime.combine(timezone.now().date(), datetime.min.time()))
        today_end = timezone.make_aware(datetime.combine(timezone.now().date(), datetime.max.time()))
        today_take_orders = TakeOrder.objects.filter(
            branch=branch,
            created_at__gte=today_start,
            created_at__lte=today_end
        )
        if not is_admin_user:
            today_take_orders = today_take_orders.filter(created_by=request.user)
        today_take_orders = today_take_orders.values('id', 'order_number', 'status', 'customer_name').order_by('-created_at')[:5]
        
        today_take_orders_data = [
            {
                'id': str(order['id']),
                'orderNumber': order['order_number'],
                'status': order['status'],
                'customerName': order['customer_name'],
            }
            for order in today_take_orders
        ]
        
        # Get recent sales (last 5 transactions from the date range)
        print(f"[Dashboard] Fetching recent sales for branch {branch.id} from {from_date} to {to_date}")
        
        # Get all orders (not just 'sale' type) since POS orders might have different type
        recent_sales = Order.objects.filter(
            branch=branch,
            created_at__gte=from_date,
            created_at__lte=to_date
        ).exclude(
            order_type__in=['pending', 'cancelled']
        )
        if not is_admin_user:
            if active_session:
                recent_sales = recent_sales.filter(session=active_session)
            else:
                recent_sales = recent_sales.none()
        recent_sales = recent_sales.values('id', 'order_number', 'total', 'payment_method', 'created_at', 'order_type').order_by('-created_at')[:5]
        
        print(f"[Dashboard] Found {recent_sales.count()} recent sales")
        
        recent_sales_data = [
            {
                'id': str(sale['id']),
                'description': f"Sale #{sale['order_number']}",
                'amount': float(sale['total']),
                'paymentMethod': sale['payment_method'],
                'createdAt': sale['created_at'].isoformat(),
            }
            for sale in recent_sales
        ]
        
        print(f"[Dashboard] Recent sales data: {recent_sales_data}")
        
        dashboard_data = {
            'kpiData': kpi_data,
            'topProducts': top_products,
            'paymentData': payment_data,
            'salesData': sales_data,
            'lowStockItems': low_stock_items_data,
            'takeOrders': today_take_orders_data,
            'recentSales': recent_sales_data,
            'activeSession': active_session_data,
        }
        
        return Response(dashboard_data)
