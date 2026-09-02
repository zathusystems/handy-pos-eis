from django.contrib.auth import get_user_model, authenticate
from django.contrib.auth.backends import ModelBackend
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import viewsets, permissions, generics, status
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework_simplejwt.tokens import RefreshToken
from .serializers import UserSerializer, RegisterSerializer, ChangePasswordSerializer

User = get_user_model()


def _is_admin_user(user):
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True

    try:
        from business.models import Business
        if Business.objects.filter(owner=user).exists():
            return True
    except Exception:
        pass

    try:
        from staff.models import Staff, StaffRole
        staff = Staff.objects.get(user=user, is_active=True)
        return staff.role == StaffRole.ADMIN
    except Exception:
        return False

class EmailOrPhoneBackend(ModelBackend):
    """Custom authentication backend that accepts both email and phone"""
    def authenticate(self, request, username=None, password=None, **kwargs):
        try:
            # Try to find user by email first
            user = User.objects.get(email=username)
        except User.DoesNotExist:
            try:
                # If not found by email, try phone
                user = User.objects.get(phone=username)
            except User.DoesNotExist:
                return None
        
        # Check password
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

class UserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        print(f"[DEBUG REGISTER] RegisterView.create() called")
        print(f"[DEBUG REGISTER] Request data: {request.data}")
        
        serializer = self.get_serializer(data=request.data)
        print(f"[DEBUG REGISTER] Serializer created: {serializer}")
        
        is_valid = serializer.is_valid(raise_exception=False)
        print(f"[DEBUG REGISTER] Serializer is_valid: {is_valid}")
        
        if not is_valid:
            print(f"[DEBUG REGISTER] Serializer errors: {serializer.errors}")
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            print(f"[DEBUG REGISTER] Serializer validation passed, saving...")
            user = serializer.save()
            print(f"[DEBUG REGISTER] User saved: {user.id}, email={user.email}, phone={user.phone}")
            
            refresh = RefreshToken.for_user(user)
            response_data = {
                'user': UserSerializer(user).data,
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            }
            print(f"[DEBUG REGISTER] Returning success response")
            return Response(response_data, status=status.HTTP_201_CREATED)
        except Exception as e:
            print(f"[DEBUG REGISTER] Error during user creation: {str(e)}")
            import traceback
            traceback.print_exc()
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

@method_decorator(csrf_exempt, name='dispatch')
class LoginView(generics.GenericAPIView):
    """
    Login endpoint - JWT based authentication
    No CSRF required for desktop/mobile apps
    """
    permission_classes = [permissions.AllowAny]

    @staticmethod
    def _resolve_staff_assignment(user):
        """
        Resolve staff assignment for this user if present.
        """
        try:
            from staff.models import Staff
            return Staff.objects.select_related('business', 'branch').filter(
                user=user,
                is_active=True
            ).first()
        except Exception:
            return None
    
    def options(self, request, *args, **kwargs):
        """Handle CORS preflight requests"""
        response = Response()
        response['Access-Control-Allow-Origin'] = '*'
        response['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return response
    
    def post(self, request):
        email = request.data.get('email')
        phone = request.data.get('phone')
        password = request.data.get('password')

        print(f"[DEBUG LOGIN] Email: {email}, Phone: {phone}, Password: {'*' * len(password) if password else 'None'}")

        if not (email or phone) or not password:
            print(f"[DEBUG LOGIN] Missing credentials")
            return Response(
                {'error': 'Provide email/phone and password'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Try to authenticate with email or phone
        user = None
        try:
            if email:
                print(f"[DEBUG LOGIN] Looking up user by email: {email}")
                user = User.objects.get(email__iexact=email)
                print(f"[DEBUG LOGIN] Found user by email: {user}")
            elif phone:
                print(f"[DEBUG LOGIN] Looking up user by phone: {phone}")
                user = User.objects.get(phone=phone)
                print(f"[DEBUG LOGIN] Found user by phone: {user}")
        except User.DoesNotExist as e:
            print(f"[DEBUG LOGIN] User not found: {e}")
            pass
        
        # Verify password
        if not user:
            print(f"[DEBUG LOGIN] User is None")
            return Response(
                {'error': 'Invalid credentials'},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        if not user.check_password(password):
            print(f"[DEBUG LOGIN] Password check failed for user: {user}")
            return Response(
                {'error': 'Invalid credentials'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        if not getattr(user, 'is_active', True):
            print(f"[DEBUG LOGIN] Inactive user attempted login: {user}")
            return Response(
                {'error': 'This account is inactive. Please contact your administrator.'},
                status=status.HTTP_403_FORBIDDEN
            )

        print(f"[DEBUG LOGIN] Login successful for user: {user}, id: {user.id}")
        
        # Check businesses for this user
        from business.models import Business
        owned_businesses = Business.objects.filter(owner=user)
        print(f"[DEBUG LOGIN] User {user.id} has {owned_businesses.count()} owned businesses")
        for biz in owned_businesses:
            print(f"[DEBUG LOGIN]   - Business: {biz.id}, name: {biz.name}")

        staff_assignment = self._resolve_staff_assignment(user)
        if staff_assignment and staff_assignment.business:
            print(
                f"[DEBUG LOGIN] User {user.id} staff assignment -> "
                f"business: {staff_assignment.business.id}, branch: "
                f"{staff_assignment.branch.id if staff_assignment.branch else 'None'}, role: {staff_assignment.role}"
            )
        elif not owned_businesses.exists():
            print(f"[DEBUG LOGIN] User {user.id} has no staff assignment")

        # Include owner businesses + assigned staff business for compatibility with clients.
        all_businesses = list(owned_businesses)
        if staff_assignment and staff_assignment.business:
            if not any(b.id == staff_assignment.business.id for b in all_businesses):
                all_businesses.append(staff_assignment.business)
        
        # Return JWT tokens instead of session
        refresh = RefreshToken.for_user(user)
        return Response({
            'user': UserSerializer(user).data,
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'businesses': [
                {
                    'id': str(b.id),
                    'name': b.name,
                    'business_type': b.business_type,
                    'tin': b.tin,
                    # Legacy aliases for older frontend payload parsers
                    'tax_pin': b.tin,
                    'taxPin': b.tin,
                }
                for b in all_businesses
            ],
            'assigned_business_id': (
                str(staff_assignment.business.id)
                if staff_assignment and staff_assignment.business
                else None
            ),
            'assigned_branch_id': (
                str(staff_assignment.branch.id)
                if staff_assignment and staff_assignment.branch
                else None
            ),
            'is_staff_user': bool(staff_assignment),
        }, status=status.HTTP_200_OK)

# Keep the old function-based view for backward compatibility
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def login(request):
    """Deprecated: Use LoginView instead"""
    view = LoginView.as_view()
    return view(request)

@api_view(['GET', 'PATCH'])
@permission_classes([permissions.IsAuthenticated])
def me(request):
    """Get or update current user profile"""
    if request.method == 'GET':
        return Response(UserSerializer(request.user).data)
    
    elif request.method == 'PATCH':
        print(f"[DEBUG ME PATCH] Updating user: {request.user.id}")
        print(f"[DEBUG ME PATCH] Request data: {request.data}")
        
        user = request.user
        
        # Update allowed fields
        allowed_fields = [
            'first_name', 'last_name', 'email', 'phone', 
            'residence_location',
            'whatsapp_number', 'address', 'city', 'state', 
            'country', 'postal_code'
        ]
        
        for field in allowed_fields:
            if field in request.data:
                setattr(user, field, request.data[field])
        
        try:
            user.save()
            print(f"[DEBUG ME PATCH] User updated successfully")
            return Response(UserSerializer(user).data, status=status.HTTP_200_OK)
        except Exception as e:
            print(f"[DEBUG ME PATCH] Error updating user: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def change_password(request):
    """Change password for the current user (admin only)"""
    if not _is_admin_user(request.user):
        return Response(
            {'error': 'You do not have permission to change passwords.'},
            status=status.HTTP_403_FORBIDDEN
        )

    serializer = ChangePasswordSerializer(
        data=request.data,
        context={'user': request.user}
    )
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    current_password = serializer.validated_data.get('current_password')
    new_password = serializer.validated_data.get('new_password')

    if not request.user.check_password(current_password):
        return Response(
            {'current_password': 'Current password is incorrect.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    request.user.set_password(new_password)
    request.user.save()
    return Response({'status': 'password_changed'}, status=status.HTTP_200_OK)
