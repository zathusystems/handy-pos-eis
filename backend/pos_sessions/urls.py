from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SessionViewSet, OrderViewSet, OrderItemViewSet, DiscountRuleViewSet
from .correction_views import VoidTransactionViewSet, CreditNoteViewSet, DebitNoteViewSet
from .sync_views import sync_push, sync_pull

router = DefaultRouter()
router.register(r'sessions', SessionViewSet, basename='session')
router.register(r'orders', OrderViewSet, basename='order')
router.register(r'order-items', OrderItemViewSet, basename='order-item')
router.register(r'discounts', DiscountRuleViewSet, basename='discount')
router.register(r'void-transactions', VoidTransactionViewSet, basename='void-transaction')
router.register(r'credit-notes', CreditNoteViewSet, basename='credit-note')
router.register(r'debit-notes', DebitNoteViewSet, basename='debit-note')

urlpatterns = [
    path('', include(router.urls)),
    # Sync endpoints
    path('sync/push/', sync_push, name='sync-push'),
    path('sync/pull/', sync_pull, name='sync-pull'),
]
