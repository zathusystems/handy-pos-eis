"""
MRA EIS URL Configuration
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    TerminalViewSet, MRAConfigurationViewSet,
    MRAInvoiceViewSet, ReceiptViewSet, OfflineInvoiceQueueViewSet,
    MRAAPIErrorViewSet, MRAProductCodesView, MRAUtilityView,
    PreparePendingPOSOrdersView
)

router = DefaultRouter()
router.register(r'terminals', TerminalViewSet, basename='terminal')
router.register(r'configurations', MRAConfigurationViewSet, basename='configuration')
router.register(r'invoices', MRAInvoiceViewSet, basename='invoice')
router.register(r'receipts', ReceiptViewSet, basename='receipt')
router.register(r'offline-queue', OfflineInvoiceQueueViewSet, basename='offline-queue')
router.register(r'api-errors', MRAAPIErrorViewSet, basename='api-error')

urlpatterns = [
    path('product-codes/', MRAProductCodesView.as_view(), name='product-codes'),
    path('utilities/<str:action_name>/', MRAUtilityView.as_view(), name='mra-utility'),
    path('pos-orders/prepare-pending/', PreparePendingPOSOrdersView.as_view(), name='prepare-pending-pos-orders'),
    path('', include(router.urls)),
]
