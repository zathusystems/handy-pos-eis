"""
Tax Rate Sync Views
Handles sync push/pull for tax rates
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone
from .models import TaxRate, Business
from .serializers import TaxRateSerializer


def _process_tax_changes(business_id, changes):
    """
    Process tax rate changes (create, update, delete)
    Returns (acknowledged_list, errors_list)
    """
    try:
        business = Business.objects.get(id=business_id)
    except Business.DoesNotExist:
        return [], [{'error': f'Business {business_id} not found'}]

    if getattr(getattr(business, 'settings', None), 'enable_eis', False):
        return [], [
            {
                'error': 'Local tax sync is disabled because MRA EIS is enabled. Use MRA product tax mappings.'
            }
        ]
    
    acknowledged = []
    errors = []
    
    for change in changes:
        try:
            entity_id = change.get('id')
            operation = change.get('op')
            change_data = change.get('data', {})
            
            print(f'[Tax Sync] Processing {operation} for tax {entity_id}')
            print(f'[Tax Sync] Change data: {change_data}')
            
            if operation == 'create':
                # Create new tax rate
                change_data['business'] = business.id
                
                # Ensure all required fields are present
                required_fields = ['name', 'rate', 'tax_type', 'effective_from', 'is_active']
                missing_fields = [f for f in required_fields if f not in change_data]
                
                if missing_fields:
                    print(f'[Tax Sync] Missing required fields: {missing_fields}')
                    print(f'[Tax Sync] Available fields: {list(change_data.keys())}')
                    errors.append({
                        'id': entity_id,
                        'error': f'Missing required fields: {missing_fields}'
                    })
                    continue
                
                print(f'[Tax Sync] Creating tax with data: {change_data}')
                print(f'[Tax Sync] Business ID: {business.id}')
                serializer = TaxRateSerializer(data=change_data)
                if serializer.is_valid():
                    instance = serializer.save()
                    print(f'[Tax Sync] Tax created successfully: {instance.id}')
                    print(f'[Tax Sync] Tax saved to database: name={instance.name}, rate={instance.rate}, business={instance.business_id}')
                    acknowledged.append({'id': entity_id})
                else:
                    print(f'[Tax Sync] Serializer errors: {serializer.errors}')
                    print(f'[Tax Sync] Serializer data: {serializer.initial_data}')
                    errors.append({
                        'id': entity_id,
                        'error': serializer.errors
                    })
            
            elif operation == 'update':
                # Update existing tax rate
                try:
                    tax_rate = TaxRate.objects.get(id=entity_id, business=business)
                    if tax_rate.locked:
                        errors.append({
                            'id': entity_id,
                            'error': 'Cannot modify a locked tax rate. Create a new tax rate instead.'
                        })
                        continue

                    serializer = TaxRateSerializer(tax_rate, data=change_data, partial=True)
                    if serializer.is_valid():
                        serializer.save()
                        acknowledged.append({'id': entity_id})
                    else:
                        errors.append({
                            'id': entity_id,
                            'error': serializer.errors
                        })
                except TaxRate.DoesNotExist:
                    errors.append({
                        'id': entity_id,
                        'error': 'Tax rate not found'
                    })
            
            elif operation == 'delete':
                # Delete tax rate
                try:
                    tax_rate = TaxRate.objects.get(id=entity_id, business=business)
                    if tax_rate.locked:
                        errors.append({
                            'id': entity_id,
                            'error': 'Cannot delete a locked tax rate. Create a new tax rate instead.'
                        })
                        continue

                    tax_rate.delete()
                    acknowledged.append({'id': entity_id})
                except TaxRate.DoesNotExist:
                    errors.append({
                        'id': entity_id,
                        'error': 'Tax rate not found'
                    })
            
            else:
                errors.append({
                    'id': entity_id,
                    'error': f'Unknown operation: {operation}'
                })
        
        except Exception as e:
            errors.append({
                'id': change.get('id'),
                'error': str(e)
            })
    
    return acknowledged, errors


def _get_tax_changes(business_id, since):
    """
    Get tax rate changes since a given timestamp
    Returns (changes_list, error)
    """
    try:
        # Get the business
        try:
            business = Business.objects.get(id=business_id)
        except Business.DoesNotExist:
            return [], f"Business {business_id} not found"
        
        # Parse the since timestamp
        try:
            since_dt = timezone.datetime.fromisoformat(since.replace('Z', '+00:00'))
        except:
            since_dt = timezone.datetime(2000, 1, 1, tzinfo=timezone.utc)
        
        # Get all tax rates for this business updated since the timestamp
        tax_rates = TaxRate.objects.filter(
            business=business,
            updated_at__gte=since_dt
        ).order_by('updated_at')
        
        serializer = TaxRateSerializer(tax_rates, many=True)
        return serializer.data, None
    
    except Exception as e:
        return [], str(e)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def sync_push(request):
    """
    Handle tax rate sync push
    """
    try:
        data = request.data
        changes = data.get('changes', [])
        business_id = data.get('business_id')
        
        if not business_id:
            return Response(
                {'error': 'business_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get the business
        try:
            business = Business.objects.get(id=business_id)
        except Business.DoesNotExist:
            return Response(
                {'error': f'Business {business_id} not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        acknowledged, errors = _process_tax_changes(business.id, changes)
        
        return Response({
            'results': {
                'acknowledged': acknowledged,
                'errors': errors
            }
        }, status=status.HTTP_200_OK)
    
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def sync_pull(request):
    """
    Handle tax rate sync pull
    """
    try:
        business_id = request.query_params.get('business_id')
        since = request.query_params.get('since', '2000-01-01T00:00:00Z')
        
        if not business_id:
            return Response(
                {'error': 'business_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        tax_rates, error = _get_tax_changes(business_id, since)
        
        if error:
            return Response(
                {'error': error},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        return Response({
            'changes': {
                'tax_rates': tax_rates
            }
        }, status=status.HTTP_200_OK)
    
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


__all__ = ['sync_push', 'sync_pull', '_get_tax_changes']
