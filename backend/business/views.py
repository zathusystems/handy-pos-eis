"""
MRA EIS-Compliant Business Views

Provides API endpoints for business operations with MRA compliance:
- Taxpayer identity management
- Branch tracking
- Tax rate immutability enforcement
- Invoice immutability enforcement
- Relational line item handling
"""

from rest_framework import viewsets, permissions, status, filters, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
import re

from .models import (
    Business, Branch, BusinessSettings, TaxRate, Invoice, InvoiceLine,
    Customer, Expense
)
from .serializers import (
    BusinessSerializer, BusinessDetailSerializer, BusinessCreateSerializer,
    BusinessUpdateSerializer, BranchSerializer, BranchCreateSerializer,
    BusinessSettingsSerializer, TaxRateSerializer, TaxRateCreateSerializer,
    TaxRateUpdateSerializer, InvoiceSerializer, InvoiceDetailSerializer,
    InvoiceCreateSerializer, InvoiceUpdateSerializer, InvoiceLineSerializer,
    CustomerSerializer, CustomerCreateSerializer, ExpenseSerializer,
    ExpenseCreateSerializer
)


# ============================================================================
# PERMISSIONS
# ============================================================================

class IsBusinessOwnerOrReadOnly(permissions.BasePermission):
    """Permission to check if user is business owner"""
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return obj.owner == request.user


def _default_main_branch_name(business_name):
    """
    Build a readable default main branch name with business initials.
    Example: "Acme Trading Limited" -> "ATL Main Branch".
    """
    parts = re.findall(r"[A-Za-z0-9]+", str(business_name or ""))
    initials = ''.join(part[0].upper() for part in parts if part)
    if not initials:
        return 'Main Branch'
    return f'{initials} Main Branch'


# ============================================================================
# BUSINESS VIEWSET
# ============================================================================

class BusinessViewSet(viewsets.ModelViewSet):
    """
    ViewSet for business management with MRA compliance.
    
    Supports:
    - List businesses
    - Create business
    - Retrieve business
    - Update business
    - Delete business
    - Add branch
    - Get settings
    - Add tax rate
    """
    permission_classes = [permissions.IsAuthenticated]
    queryset = Business.objects.all()
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'tin', 'email']
    ordering_fields = ['name', 'created_at', 'mra_enrolled']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter businesses by owner or staff assignment"""
        from staff.models import Staff
        
        # Get businesses owned by user
        owned_businesses = Business.objects.filter(owner=self.request.user)
        
        # Get businesses where user is a staff member
        try:
            staff = Staff.objects.get(user=self.request.user)
            if staff.business:
                # Combine owned businesses with assigned business
                return Business.objects.filter(
                    id__in=list(owned_businesses.values_list('id', flat=True)) + [staff.business.id]
                ).distinct()
        except Staff.DoesNotExist:
            pass
        
        return owned_businesses

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action == 'create':
            return BusinessCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return BusinessUpdateSerializer
        elif self.action == 'retrieve':
            return BusinessDetailSerializer
        return BusinessSerializer

    @transaction.atomic
    def perform_create(self, serializer):
        """Create business with auto-setup"""
        business = serializer.save(owner=self.request.user)

        # Auto-create settings
        biz_settings = BusinessSettings.objects.create(business=business)
        requested_currency = str(getattr(serializer, '_requested_currency', '') or '').strip().upper()
        if requested_currency in {'USD', 'MWK'} and biz_settings.currency != requested_currency:
            biz_settings.currency = requested_currency
            biz_settings.save(update_fields=['currency'])

        # Auto-create default main branch
        main_branch = Branch.objects.create(
            business=business,
            name=_default_main_branch_name(business.name),
            address='',
            city='',
            country='',
            is_active=True
        )

        # Auto-add business creator as Admin staff member
        try:
            from staff.models import Staff, StaffRole
            user_name = ''
            if self.request.user.first_name or self.request.user.last_name:
                user_name = f"{self.request.user.first_name} {self.request.user.last_name}".strip()
            user_name = user_name or self.request.user.email or self.request.user.phone or 'Admin'

            if self.request.user.email:
                staff, created = Staff.objects.get_or_create(
                    user=self.request.user,
                    defaults={
                        'business': business,
                        'branch': main_branch,
                        'name': user_name,
                        'email': self.request.user.email,
                        'phone': self.request.user.phone or '',
                        'role': 'Admin',
                        'is_active': True
                    }
                )
                if not created:
                    # Keep owner's staff profile aligned to the newly created business.
                    # Without this, existing profiles may keep a null/old branch assignment.
                    fields_to_update = []

                    if staff.business_id != business.id:
                        staff.business = business
                        fields_to_update.append('business')

                    if staff.branch_id != main_branch.id:
                        staff.branch = main_branch
                        fields_to_update.append('branch')

                    if staff.role != StaffRole.ADMIN:
                        staff.role = StaffRole.ADMIN
                        fields_to_update.append('role')

                    if not staff.is_active:
                        staff.is_active = True
                        fields_to_update.append('is_active')

                    if fields_to_update:
                        staff.save(update_fields=fields_to_update)
        except Exception as e:
            # Don't fail business creation if staff creation fails
            pass

    def perform_update(self, serializer):
        """Update business"""
        serializer.save()

    @action(detail=True, methods=['post'])
    def add_branch(self, request, pk=None):
        """Add a new branch to business"""
        business = self.get_object()
        serializer = BranchCreateSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(business=business)
            return Response(
                BranchSerializer(serializer.instance).data,
                status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get', 'put'])
    def business_settings(self, request, pk=None):
        """Get or update business settings"""
        business = self.get_object()
        biz_settings = business.settings

        if request.method == 'PUT':
            serializer = BusinessSettingsSerializer(
                biz_settings,
                data=request.data,
                partial=True
            )
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer = BusinessSettingsSerializer(biz_settings)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def branches(self, request, pk=None):
        """Get all branches for business"""
        business = self.get_object()
        branches = business.branches.filter(is_active=True)
        serializer = BranchSerializer(branches, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def sync_eis_branches(self, request, pk=None):
        """Mirror MRA EIS sites into local branches for this business."""
        business = self.get_object()
        try:
            from mra_eis.services import EISBranchSyncService

            result = EISBranchSyncService.sync_for_business(business)
            branches = business.branches.filter(is_active=True)
            return Response(
                {
                    **result,
                    'branches': BranchSerializer(branches, many=True).data,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as exc:
            return Response(
                {'error': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=True, methods=['post'])
    def add_tax_rate(self, request, pk=None):
        """Add a new tax rate to business"""
        business = self.get_object()
        serializer = TaxRateCreateSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(business=business, created_by=request.user)
            return Response(
                TaxRateSerializer(serializer.instance).data,
                status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def tax_rates(self, request, pk=None):
        """Get all tax rates for business"""
        business = self.get_object()
        tax_rates = business.tax_rates.filter(is_active=True)
        serializer = TaxRateSerializer(tax_rates, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def mra_status(self, request, pk=None):
        """Get MRA enrollment status"""
        business = self.get_object()
        return Response({
            'tin': business.tin,
            'vat_registered': business.vat_registered,
            'mra_taxpayer_type': business.mra_taxpayer_type,
            'mra_enrolled': business.mra_enrolled,
            'mra_enrolled_at': business.mra_enrolled_at,
        })


# ============================================================================
# BRANCH VIEWSET
# ============================================================================

class BranchViewSet(viewsets.ModelViewSet):
    """
    ViewSet for branch management with MRA tracking.
    
    Supports:
    - List branches
    - Create branch
    - Retrieve branch
    - Update branch
    - Delete branch
    """
    serializer_class = BranchSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'city', 'mra_branch_code']
    ordering_fields = ['name', 'created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter branches by business owner"""
        return Branch.objects.filter(business__owner=self.request.user)

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action in ['create', 'update', 'partial_update']:
            return BranchCreateSerializer
        return BranchSerializer

    def perform_create(self, serializer):
        """Create branch"""
        serializer.save()


# ============================================================================
# TAX RATE VIEWSET
# ============================================================================

class TaxRateViewSet(viewsets.ModelViewSet):
    """
    ViewSet for tax rate management with immutability enforcement.
    
    CRITICAL: Tax rates are locked after use and cannot be modified.
    
    Supports:
    - List tax rates
    - Create tax rate
    - Retrieve tax rate
    - Update tax rate (with immutability check)
    - Delete tax rate
    - Set default tax rate
    """
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'tax_type', 'mra_tax_code']
    ordering_fields = ['name', 'rate', 'is_default', 'created_at']
    ordering = ['-is_default', '-created_at']

    def _is_business_eis_enabled(self, business: Business) -> bool:
        return bool(getattr(getattr(business, 'settings', None), 'enable_eis', False))

    def _ensure_local_tax_management_allowed(self, business: Business) -> None:
        if self._is_business_eis_enabled(business):
            raise serializers.ValidationError({
                'error': 'Local tax management is disabled because MRA EIS is enabled. Use MRA product tax mappings.'
            })

    def get_queryset(self):
        """Filter tax rates by business owner"""
        return TaxRate.objects.filter(business__owner=self.request.user)

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action == 'create':
            return TaxRateCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return TaxRateUpdateSerializer
        return TaxRateSerializer

    def perform_create(self, serializer):
        """Create tax rate"""
        business = self.request.user.businesses.first()
        if not business:
            raise serializers.ValidationError('User must have a business')
        self._ensure_local_tax_management_allowed(business)
        serializer.save(business=business, created_by=self.request.user)

    def _ensure_tax_rate_mutable(self, tax_rate: TaxRate) -> None:
        """Prevent updates/deletes for locked tax rates."""
        if tax_rate.locked:
            raise serializers.ValidationError(
                {'error': 'Cannot modify a locked tax rate. Create a new tax rate instead.'}
            )

    def perform_update(self, serializer):
        """Update tax rate with immutability check"""
        self._ensure_local_tax_management_allowed(serializer.instance.business)
        self._ensure_tax_rate_mutable(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        """Delete tax rate with immutability check"""
        self._ensure_local_tax_management_allowed(instance.business)
        self._ensure_tax_rate_mutable(instance)
        super().perform_destroy(instance)

    @action(detail=True, methods=['post'])
    def set_default(self, request, pk=None):
        """Set this tax rate as default for business"""
        tax_rate = self.get_object()
        self._ensure_local_tax_management_allowed(tax_rate.business)

        # Unset all other defaults for this business
        TaxRate.objects.filter(business=tax_rate.business).update(is_default=False)

        # Set this one as default
        tax_rate.is_default = True
        tax_rate.save()

        return Response(TaxRateSerializer(tax_rate).data)

    @action(detail=False, methods=['get'])
    def active(self, request):
        """Get all active tax rates"""
        tax_rates = self.get_queryset().filter(is_active=True)
        serializer = self.get_serializer(tax_rates, many=True)
        return Response(serializer.data)


# ============================================================================
# CUSTOMER VIEWSET
# ============================================================================

class CustomerViewSet(viewsets.ModelViewSet):
    """
    ViewSet for customer management with VAT tracking.
    
    Supports:
    - List customers
    - Create customer
    - Retrieve customer
    - Update customer
    - Delete customer
    """
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'email', 'customer_tin']
    ordering_fields = ['name', 'created_at', 'vat_registered']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter customers by business owner"""
        return Customer.objects.filter(business__owner=self.request.user)

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action in ['create', 'update', 'partial_update']:
            return CustomerCreateSerializer
        return CustomerSerializer

    def perform_create(self, serializer):
        """Create customer"""
        business = self.request.user.businesses.first()
        if not business:
            raise serializers.ValidationError('User must have a business')
        serializer.save(business=business)


# ============================================================================
# INVOICE VIEWSET
# ============================================================================

class InvoiceViewSet(viewsets.ModelViewSet):
    """
    ViewSet for invoice management with MRA EIS compliance.
    
    CRITICAL: Invoices are immutable after payment or MRA submission.
    
    Supports:
    - List invoices
    - Create invoice (with line items)
    - Retrieve invoice
    - Update invoice (with immutability check)
    - Delete invoice
    - Submit to MRA
    - Get invoice lines
    """
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['invoice_number', 'customer_name', 'mra_invoice_number']
    ordering_fields = ['invoice_number', 'status', 'mra_status', 'created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter invoices by business owner"""
        return Invoice.objects.filter(business__owner=self.request.user)

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action == 'create':
            return InvoiceCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return InvoiceUpdateSerializer
        elif self.action == 'retrieve':
            return InvoiceDetailSerializer
        return InvoiceSerializer

    @transaction.atomic
    def perform_create(self, serializer):
        """Create invoice"""
        business = self.request.user.businesses.first()
        if not business:
            raise serializers.ValidationError('User must have a business')
        serializer.save(business=business)

    def perform_update(self, serializer):
        """Update invoice with immutability check"""
        try:
            serializer.save()
        except DjangoValidationError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['get'])
    def lines(self, request, pk=None):
        """Get all line items for invoice"""
        invoice = self.get_object()
        lines = invoice.lines.all()
        serializer = InvoiceLineSerializer(lines, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def submit_to_mra(self, request, pk=None):
        """Submit invoice to MRA"""
        invoice = self.get_object()

        # Check if already submitted
        if invoice.mra_status == 'SUBMITTED':
            return Response(
                {'error': 'Invoice already submitted to MRA'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if locked
        if invoice.is_locked:
            return Response(
                {'error': 'Cannot submit locked invoice'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # TODO: Integrate with MRA EIS API
        # For now, just update status
        invoice.mra_status = 'SUBMITTED'
        invoice.is_locked = True
        invoice.save()

        return Response(
            InvoiceSerializer(invoice).data,
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'])
    def mark_paid(self, request, pk=None):
        """Mark invoice as paid (locks it)"""
        invoice = self.get_object()

        if invoice.is_locked:
            return Response(
                {'error': 'Invoice is already locked'},
                status=status.HTTP_400_BAD_REQUEST
            )

        invoice.status = 'Paid'
        invoice.is_locked = True
        invoice.save()

        return Response(
            InvoiceSerializer(invoice).data,
            status=status.HTTP_200_OK
        )

    @action(detail=False, methods=['get'])
    def pending_mra_submission(self, request):
        """Get invoices pending MRA submission"""
        invoices = self.get_queryset().filter(mra_status='PENDING')
        serializer = self.get_serializer(invoices, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def mra_submitted(self, request):
        """Get invoices submitted to MRA"""
        invoices = self.get_queryset().filter(mra_status='SUBMITTED')
        serializer = self.get_serializer(invoices, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def locked(self, request):
        """Get locked invoices"""
        invoices = self.get_queryset().filter(is_locked=True)
        serializer = self.get_serializer(invoices, many=True)
        return Response(serializer.data)


# ============================================================================
# EXPENSE VIEWSET
# ============================================================================

class ExpenseViewSet(viewsets.ModelViewSet):
    """
    ViewSet for expense management.
    
    Supports:
    - List expenses
    - Create expense
    - Retrieve expense
    - Update expense
    - Delete expense
    - Filter by branch
    """
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'category']
    ordering_fields = ['title', 'amount', 'date', 'status', 'created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter expenses by business owner and optionally by branch"""
        queryset = Expense.objects.filter(business__owner=self.request.user)
        
        # Filter by branch if provided in query parameters
        branch_id = self.request.query_params.get('branch', None)
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        
        return queryset

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action in ['create', 'update', 'partial_update']:
            return ExpenseCreateSerializer
        return ExpenseSerializer

    def perform_create(self, serializer):
        """Create expense"""
        business = self.request.user.businesses.first()
        if not business:
            raise serializers.ValidationError('User must have a business')
        serializer.save(
            business=business,
            created_by=self.request.user.email,
            status='Approved',
            approved_by=self.request.user.email,
            approved_at=timezone.now(),
        )
