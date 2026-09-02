

'use client';

import React, { useCallback, useState, useEffect, useMemo } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  Boxes,
  LayoutDashboard,
  Package,
  Settings,
  ShoppingBag,
  Users,
  Download,
  ChevronDown,
  Bell,
  Plus,
  FileText,
  Truck,
  Building,
  Printer,
  Archive,
  AlertTriangle,
  Pill,
  MonitorPlay,
  BarChart2,
  BookOpen,
  ClipboardList,
  ChefHat,
  Loader2,
  Edit,
  History,
  CreditCard,
  BookUser,
  FileSignature,
  ShieldCheck,
  Lock,
  UserCheck,
  Share2,
  Group,
  Utensils,
  RefreshCw,
  Trash2,
  CheckCircle2,
  Zap,
} from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useForm, FormProvider } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import * as z from 'zod';
import { useLiveQuery } from 'dexie-react-hooks';
import { format, parseISO, isBefore, addDays } from 'date-fns';

import {
  Sidebar,
  SidebarContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
  SidebarProvider,
  SidebarTrigger,
  SidebarFooter,
  SidebarSeparator,
  useSidebar,
} from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';
import { HandyPosLogo } from '@/components/icons/logo';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu';
import { DashboardHeader } from '@/components/dashboard-header';
import { Input } from '@/components/ui/input';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { useSyncStatus } from '@/hooks/use-sync-status';
import { X } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Badge } from '@/components/ui/badge';
import { ThemeCustomizer } from '@/components/theme-customizer';
import { syncBusinessBranchesFromServer } from '@/lib/branch-sync';
import { formatInventoryQuantity, formatNotificationBadgeCount } from '@/lib/quantity-format';
import { isWarehouseBranchId, WAREHOUSE_BRANCH, WAREHOUSE_BRANCH_ID } from '@/lib/branch-context';

// Helper to remove auth sync items from queue
const removeAuthSyncItem = (itemId: string) => {
  const SYNC_QUEUE_KEY = 'handypos-sync-queue';
  try {
    // Remove from localStorage
    const stored = localStorage.getItem(SYNC_QUEUE_KEY);
    if (stored) {
      const queue = JSON.parse(stored);
      const filtered = queue.filter((item: any) => item.id !== itemId);
      localStorage.setItem(SYNC_QUEUE_KEY, JSON.stringify(filtered));
      console.log('[SyncQueue] Removed auth item from persistent queue:', itemId);
    }
    
    // Also need to clear from authFetch's internal queue
    // Since we can't directly access authFetch's private queue, we'll mark it as cancelled
    // by storing a list of cancelled items
    const CANCELLED_KEY = 'handypos-cancelled-sync-items';
    const cancelled = JSON.parse(localStorage.getItem(CANCELLED_KEY) || '[]');
    if (!cancelled.includes(itemId)) {
      cancelled.push(itemId);
      localStorage.setItem(CANCELLED_KEY, JSON.stringify(cancelled));
    }
  } catch (e) {
    console.error('[SyncQueue] Failed to remove item from queue:', e);
  }
};

const toBackendBranchId = (id: string): string => {
  const normalized = String(id || '').trim();
  if (!normalized) return normalized;
  if (isWarehouseBranchId(normalized)) return normalized;

  const brnMatch = /^BRN-(\d+)$/i.exec(normalized);
  if (brnMatch) return brnMatch[1];

  const legacyBranchMatch = /^branch-(\d+)$/i.exec(normalized);
  if (legacyBranchMatch) return legacyBranchMatch[1];

  if (/^\d+$/.test(normalized)) return normalized;
  return normalized;
};

const getBranchIdCandidates = (branchId?: string | null): string[] => {
  const normalized = String(branchId || '').trim();
  if (!normalized) return [];
  if (isWarehouseBranchId(normalized)) return [WAREHOUSE_BRANCH_ID];

  const backendId = toBackendBranchId(normalized);
  const candidates = new Set<string>([normalized, backendId]);

  if (/^\d+$/.test(backendId)) {
    candidates.add(`BRN-${backendId}`);
    candidates.add(`branch-${backendId}`);
  }

  return Array.from(candidates).filter((candidate) => candidate.length > 0);
};

const normalizeApiBranchId = (value?: unknown): string => toBackendBranchId(String(value || '').trim());

const extractApiList = <T,>(payload: any): T[] => {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.results)) return payload.results;
  if (Array.isArray(payload?.data)) return payload.data;
  return [];
};

const getApiBranchId = (item: any): string => {
  const rawBranch = item?.branch;
  if (rawBranch && typeof rawBranch === 'object') {
    return String(rawBranch.id ?? rawBranch.pk ?? rawBranch.branch_id ?? rawBranch.branchId ?? '').trim();
  }
  return String(rawBranch ?? item?.branch_id ?? item?.branchId ?? '').trim();
};

const getApiDeviceSerial = (item: any): string => {
  return String(item?.device_serial ?? item?.deviceSerial ?? item?.mac_address ?? item?.macAddress ?? '').trim();
};

const parseStoredJson = <T,>(key: string): T | null => {
  if (typeof window === 'undefined') return null;

  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
};

const readBooleanFlag = (value: unknown): boolean | null => {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (value === 1) return true;
    if (value === 0) return false;
  }
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (['1', 'true', 'yes', 'enabled'].includes(normalized)) return true;
    if (['0', 'false', 'no', 'disabled'].includes(normalized)) return false;
  }
  return null;
};

const resolveCachedEisEnabled = (businessId: string, business?: any): boolean | null => {
  const storedBusiness =
    parseStoredJson<any>('handy-pos-business') ??
    parseStoredJson<any>('handypos-business') ??
    null;
  const storedSettings = parseStoredJson<any>('handypos-business-settings');

  const settingsBusinessId = String(storedSettings?.businessId ?? storedSettings?.business_id ?? '').trim();
  const settingsBelongToBusiness = !settingsBusinessId || settingsBusinessId === String(businessId);

  const candidates = [
    business?.enable_eis,
    business?.enableEis,
    business?.eis_enabled,
    business?.eisEnabled,
    storedBusiness?.enable_eis,
    storedBusiness?.enableEis,
    storedBusiness?.eis_enabled,
    storedBusiness?.eisEnabled,
    settingsBelongToBusiness ? storedSettings?.enableEis : undefined,
    settingsBelongToBusiness ? storedSettings?.enable_eis : undefined,
    settingsBelongToBusiness ? storedSettings?.eis_enabled : undefined,
    settingsBelongToBusiness ? storedSettings?.eisEnabled : undefined,
  ];

  for (const value of candidates) {
    const parsed = readBooleanFlag(value);
    if (parsed !== null) return parsed;
  }

  return null;
};

const getTerminalStorageKeys = (businessId: string, branchId?: string | null): string[] => {
  const keys = new Set<string>();
  for (const candidate of getBranchIdCandidates(branchId)) {
    keys.add(`handypos-terminal:${businessId}:${candidate}`);
  }
  return Array.from(keys);
};

const isActivatedTerminalForDevice = (
  terminal: any,
  normalizedBranchId: string,
  currentDeviceSerial: string
): boolean => {
  if (!terminal || typeof terminal !== 'object') return false;
  if (String(terminal.status || '').toLowerCase() !== 'active') return false;

  const terminalDeviceSerial = getApiDeviceSerial(terminal).toLowerCase();
  if (!terminalDeviceSerial || terminalDeviceSerial !== currentDeviceSerial.toLowerCase()) {
    return false;
  }

  const terminalBranchId = normalizeApiBranchId(getApiBranchId(terminal));
  return !terminalBranchId || terminalBranchId === normalizedBranchId;
};

const readCachedActivatedTerminal = (
  businessId: string,
  branchId: string,
  currentDeviceSerial: string
): any | null => {
  if (typeof window === 'undefined') return null;

  const normalizedBranchId = normalizeApiBranchId(branchId);
  for (const key of getTerminalStorageKeys(businessId, branchId)) {
    const terminal = parseStoredJson<any>(key);
    if (isActivatedTerminalForDevice(terminal, normalizedBranchId, currentDeviceSerial)) {
      return terminal;
    }
  }

  return null;
};

const persistCachedTerminal = (businessId: string, branchId: string, terminal: any): void => {
  if (typeof window === 'undefined' || !terminal) return;

  for (const key of getTerminalStorageKeys(businessId, branchId)) {
    try {
      localStorage.setItem(key, JSON.stringify(terminal));
    } catch (error) {
      console.warn('[Dashboard] Failed to persist EIS terminal cache:', error);
    }
  }
};

// Helper to clear all failed order sync items
const clearFailedOrders = () => {
  const SYNC_QUEUE_KEY = 'handypos-sync-queue';
  const CANCELLED_KEY = 'handypos-cancelled-sync-items';
  try {
    const stored = localStorage.getItem(SYNC_QUEUE_KEY);
    if (stored) {
      const queue = JSON.parse(stored);
      // Filter out all failed order items (POST to /sessions/orders/)
      const filtered = queue.filter((item: any) => 
        !item.url?.includes('/sessions/orders/') || item.error === undefined
      );
      
      // Add failed orders to cancelled list
      const failedOrders = queue.filter((item: any) => 
        item.url?.includes('/sessions/orders/') && item.error
      );
      
      const cancelled = JSON.parse(localStorage.getItem(CANCELLED_KEY) || '[]');
      failedOrders.forEach((order: any) => {
        if (!cancelled.includes(order.id)) {
          cancelled.push(order.id);
        }
      });
      
      localStorage.setItem(SYNC_QUEUE_KEY, JSON.stringify(filtered));
      localStorage.setItem(CANCELLED_KEY, JSON.stringify(cancelled));
      console.log('[SyncQueue] Cleared', failedOrders.length, 'failed orders from queue');
    }
  } catch (e) {
    console.error('[SyncQueue] Failed to clear failed orders:', e);
  }
};
import { useAuth, type User } from '@/hooks/use-auth';
import { useRBAC } from '@/hooks/use-rbac';
import { useToast } from '@/hooks/use-toast';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { db, type Subscription, type InventoryItem, type PurchaseRecord } from '@/lib/db';
import { plans } from '@/lib/subscriptions';
import { cn } from '@/lib/utils';
import { PosModal } from '@/components/pos/pos-modal';
import { TerminalActivationDialog } from '@/components/mra-eis/terminal-activation-dialog';
import { authFetch } from '@/lib/auth-fetch';
import {
  DEVICE_IDENTITY_CHANGED_EVENT,
  ensureTauriDeviceIdentity,
  getDeviceSerial,
} from '@/lib/device-identity';
import { syncSessionSnapshotToDesktopStore } from '@/lib/desktop-session-store';

import type { Permission } from '@/lib/rbac/permissions';
import { hasPermission as checkPermission } from '@/lib/rbac/permissions';

const navItems = [
    { href: '/dashboard', icon: LayoutDashboard, label: 'Dashboard', permission: 'view_dashboard' as Permission },
    { href: '/dashboard/branches', icon: Building, label: 'Branches', permission: 'manage_settings' as Permission },
    { href: '/dashboard/pos', icon: MonitorPlay, label: 'POS', permission: 'access_pos' as Permission },
    { href: '/dashboard/sessions', icon: History, label: 'Sessions', permission: 'view_sessions' as Permission },
    { href: '/dashboard/eis-sales', icon: FileText, label: 'Sales', permission: 'view_sessions' as Permission },
    { href: '/dashboard/sales', icon: BarChart2, label: 'Reports', permission: 'view_reports' as Permission },
    { href: '/dashboard/expenses', icon: CreditCard, label: 'Expenses', permission: 'view_expenses' as Permission },
    { href: '/dashboard/inventory', icon: Boxes, label: 'Inventory', permission: 'view_inventory' as Permission },
    { href: '/dashboard/suppliers', icon: Truck, label: 'Suppliers', permission: 'view_suppliers' as Permission },
    { href: '/dashboard/staff', icon: Users, label: 'Staff', permission: 'manage_staff' as Permission },
];

const settingsNav = [
  { href: '/dashboard/settings', icon: Settings, label: 'Settings', permission: 'manage_settings' as Permission },
  { href: '/dashboard/audit', icon: UserCheck, label: 'Audit Log', permission: 'view_audit_log' as Permission },
];

const EIS_ACTIVATION_PATH = '/dashboard/settings/eis';
const EIS_TERMINAL_ACTIVATION_CHANGED_EVENT = 'handypos-eis-terminal-activation-changed';
const EIS_TERMINAL_ACTIVATION_REQUESTED_EVENT = 'handypos-eis-terminal-activation-requested';

type EisActivationGateState = {
  checking: boolean;
  required: boolean;
  reason: string;
};

const profileSchema = z.object({
  displayName: z.string().min(2, 'Display name must be at least 2 characters.'),
  email: z.string().email(),
});

type ProfileFormValues = z.infer<typeof profileSchema>;

const passwordSchema = z.object({
  currentPassword: z.string().min(1, 'Current password is required.'),
  newPassword: z.string().min(8, 'Password must be at least 8 characters.'),
  confirmPassword: z.string().min(1, 'Please confirm your password.'),
}).refine((data) => data.newPassword === data.confirmPassword, {
  message: 'Passwords do not match.',
  path: ['confirmPassword'],
});

type PasswordFormValues = z.infer<typeof passwordSchema>;

function UserProfileModal({ user, isOpen, onOpenChange }: { user: User, isOpen: boolean, onOpenChange: (open: boolean) => void }) {
  const { login: updateUser } = useAuth();
  const { toast } = useToast();
  const [isEditing, setIsEditing] = useState(false);
  const [isChangingPassword, setIsChangingPassword] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [isSavingPassword, setIsSavingPassword] = useState(false);
  const getInitials = (value?: string) => {
    const text = (value || '').trim();
    if (!text) return 'U';

    const normalized = text.includes('@') ? text.split('@')[0] : text;
    const parts = normalized.replace(/[_\-.]+/g, ' ').split(/\s+/).filter(Boolean);

    if (parts.length === 1) {
      return parts[0].slice(0, 2).toUpperCase();
    }

    return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
  };
  const userInitials = getInitials(user.displayName || user.email);

  const form = useForm<ProfileFormValues>({
    resolver: zodResolver(profileSchema),
    defaultValues: {
      displayName: user?.displayName || '',
      email: user?.email || '',
    },
  });

  const passwordForm = useForm<PasswordFormValues>({
    resolver: zodResolver(passwordSchema),
    defaultValues: {
      currentPassword: '',
      newPassword: '',
      confirmPassword: '',
    },
  });
  
  const { reset } = form;
  const { reset: resetPasswordForm } = passwordForm;
  const canChangePassword = user.role === 'Admin';

  useEffect(() => {
    if (isOpen) {
      reset({
        displayName: user.displayName || '',
        email: user.email || '',
      });
      resetPasswordForm({
        currentPassword: '',
        newPassword: '',
        confirmPassword: '',
      });
    }
  }, [user, isOpen, reset, resetPasswordForm]);

  const onSubmit = (data: ProfileFormValues) => {
    setIsSaving(true);
    // Simulate API call
    setTimeout(() => {
      const updatedUserData = { ...user, ...data };
      updateUser(updatedUserData);
      setIsSaving(false);
      setIsEditing(false);
    }, 1000);
  };

  const onChangePassword = async (data: PasswordFormValues) => {
    if (!canChangePassword) {
      toast({
        variant: 'destructive',
        title: 'Permission denied',
        description: 'Only admins can change passwords.',
      });
      return;
    }

    setIsSavingPassword(true);
    try {
      await authFetch.fetch('/accounts/change-password/', {
        method: 'POST',
        body: JSON.stringify({
          current_password: data.currentPassword,
          new_password: data.newPassword,
          confirm_password: data.confirmPassword,
        }),
      });
      toast({
        title: 'Password updated',
        description: 'Your password has been changed successfully.',
      });
      resetPasswordForm({
        currentPassword: '',
        newPassword: '',
        confirmPassword: '',
      });
      setIsChangingPassword(false);
    } catch (error: any) {
      toast({
        variant: 'destructive',
        title: 'Password update failed',
        description: error?.message || 'Could not change password. Please try again.',
      });
    } finally {
      setIsSavingPassword(false);
    }
  };
  
  const handleOpenChange = (open: boolean) => {
      onOpenChange(open);
      if (!open) {
          setIsEditing(false); // Reset edit state when modal closes
          setIsChangingPassword(false);
      }
  }

  if (!user) return null;

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>My Profile</DialogTitle>
          <DialogDescription>
            {isEditing ? 'Edit your personal information.' : 'View your personal information.'}
          </DialogDescription>
        </DialogHeader>
        
        {isEditing ? (
          <FormProvider {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
              <div className="flex flex-col items-center gap-4 py-4">
                <Avatar className="h-24 w-24">
                  <AvatarFallback className="text-2xl font-semibold">{userInitials}</AvatarFallback>
                </Avatar>
              </div>
              <FormField
                control={form.control}
                name="displayName"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Display Name</FormLabel>
                    <FormControl>
                      <Input placeholder="Your name" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="email"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Email Address</FormLabel>
                    <FormControl>
                      <Input placeholder="Your email" {...field} readOnly disabled />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <DialogFooter className="pt-4">
                <Button variant="ghost" onClick={() => setIsEditing(false)}>Cancel</Button>
                <Button type="submit" disabled={isSaving}>
                  {isSaving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Save Changes
                </Button>
              </DialogFooter>
            </form>
          </FormProvider>
        ) : (
          <div>
            <div className="flex flex-col items-center gap-4 py-4">
              <Avatar className="h-24 w-24">
                <AvatarFallback className="text-2xl font-semibold">{userInitials}</AvatarFallback>
              </Avatar>
              <div className="text-center">
                <h2 className="text-xl font-semibold">{user.displayName || user.email}</h2>
                <p className="text-muted-foreground">{user.email}</p>
              </div>
            </div>
            <div className="space-y-3 text-sm">
                <div className="flex justify-between">
                    <span className="text-muted-foreground">Role:</span>
                    <span className="font-medium">{user.role}</span>
                </div>
                <div className="flex justify-between">
                    <span className="text-muted-foreground">Branch:</span>
                    <span className="font-medium">Main Branch</span>
                </div>
                 <div className="flex justify-between">
                    <span className="text-muted-foreground">Last Login:</span>
                    <span className="font-medium">
                      {new Date().toLocaleString('en-US', {
                        dateStyle: 'medium',
                        timeStyle: 'short',
                      })}
                    </span>
                </div>
            </div>
            {canChangePassword && (
              <div className="mt-6 rounded-lg border p-4">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium">Change Password</p>
                    <p className="text-xs text-muted-foreground">Admin-only action</p>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setIsChangingPassword((prev) => !prev)}
                  >
                    {isChangingPassword ? 'Cancel' : 'Change'}
                  </Button>
                </div>
                {isChangingPassword && (
                  <FormProvider {...passwordForm}>
                    <form onSubmit={passwordForm.handleSubmit(onChangePassword)} className="mt-4 space-y-3">
                      <FormField
                        control={passwordForm.control}
                        name="currentPassword"
                        render={({ field }) => (
                          <FormItem>
                            <FormLabel>Current Password</FormLabel>
                            <FormControl>
                              <Input type="password" {...field} />
                            </FormControl>
                            <FormMessage />
                          </FormItem>
                        )}
                      />
                      <FormField
                        control={passwordForm.control}
                        name="newPassword"
                        render={({ field }) => (
                          <FormItem>
                            <FormLabel>New Password</FormLabel>
                            <FormControl>
                              <Input type="password" {...field} />
                            </FormControl>
                            <FormMessage />
                          </FormItem>
                        )}
                      />
                      <FormField
                        control={passwordForm.control}
                        name="confirmPassword"
                        render={({ field }) => (
                          <FormItem>
                            <FormLabel>Confirm Password</FormLabel>
                            <FormControl>
                              <Input type="password" {...field} />
                            </FormControl>
                            <FormMessage />
                          </FormItem>
                        )}
                      />
                      <div className="pt-2">
                        <Button type="submit" disabled={isSavingPassword}>
                          {isSavingPassword && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                          Update Password
                        </Button>
                      </div>
                    </form>
                  </FormProvider>
                )}
              </div>
            )}
            <DialogFooter className="pt-6">
              <Button variant="outline" className="w-full" onClick={() => setIsEditing(true)}>
                <Edit className="mr-2 h-4 w-4" /> Edit Profile
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

const LOCAL_STORAGE_KEYS = {
    BRANCHES: 'handypos-branches',
    ACTIVE_BRANCH: 'handypos-active-branch',
    AUTH_TOKENS: 'handypos-auth-tokens',
    LEGACY_AUTH_TOKENS: 'handy-pos-auth-tokens',
};

type Branch = {
  id: string;
  name: string;
  address: string;
  mraBranchCode?: string;
  mra_branch_code?: string;
  isWarehouse?: boolean;
};

const getInitialBranches = (): Branch[] => {
    if (typeof window === 'undefined') {
        return [];
    }

    let branches: Branch[];
    try {
        const storedBranches = localStorage.getItem(LOCAL_STORAGE_KEYS.BRANCHES);
        branches = storedBranches ? JSON.parse(storedBranches) : [];
    } catch (e) {
        branches = [];
        console.error("Failed to parse branches from localStorage", e);
    }
    
    return branches;
};

const normalizePath = (path: string): string => {
  if (!path) return '/';
  return path.length > 1 ? path.replace(/\/+$/, '') : path;
};

const isNavItemActive = (pathname: string, href: string): boolean => {
  const currentPath = normalizePath(pathname);
  const targetPath = normalizePath(href);

  if (targetPath === '/dashboard') {
    return currentPath === '/dashboard';
  }

  return currentPath === targetPath || currentPath.startsWith(`${targetPath}/`);
};


function SyncQueueDropdown({ branchId }: { branchId: string | null }) {
  const [syncQueue, setSyncQueue] = useState<any[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const { dirtyRecords } = useSyncStatus(branchId);

  useEffect(() => {
    const updateQueue = () => {
      // Get list of cancelled items
      const CANCELLED_KEY = 'handypos-cancelled-sync-items';
      const cancelled = JSON.parse(localStorage.getItem(CANCELLED_KEY) || '[]');
      
      // Authenticated request queue (settings, sessions, etc.)
      const status = authFetch.getSyncQueueStatus();
      const authItems = (status.items || [])
        .filter((item: any) => !cancelled.includes(item.id)) // Filter out cancelled items
        .map((item: any) => {
          // Extract entity type from metadata or URL
          const entityType = item.entityType || item.domain || 'item';
          const entityId = item.entityId || item.url?.split('/').pop() || 'unknown';
          
          return {
            source: 'auth',
            id: item.id,
            method: item.method,
            url: item.url,
            retries: item.retries,
            error: item.error,
            entityType,
            entityId,
            domain: item.domain,
          };
        });

      // Map dirty records to display format
      const dirtyItems = (dirtyRecords || []).map((record: any) => {
        const operationMap: { [key: string]: string } = {
          'create': 'POST',
          'update': 'PUT',
          'delete': 'DELETE',
        };
        
        return {
          source: 'dirty',
          id: record.id,
          method: operationMap[record.operation] || 'UPDATE',
          url: `/api/inventory/${record.type.toLowerCase()}/${record.id}`,
          retries: 0,
          error: null,
          entityType: record.type,
          entityId: record.id,
          name: record.name,
        };
      });

      setSyncQueue([...dirtyItems, ...authItems]);
    };

    updateQueue();
    const interval = setInterval(updateQueue, 1000);
    return () => clearInterval(interval);
  }, [dirtyRecords, branchId]);

  const getActionLabel = (item: any) => {
    const action = item.method?.toUpperCase() || 'UNKNOWN';
    
    // Get entity type label
    let entityLabel = 'Item';
    if (item.entityType) {
      entityLabel = item.entityType;
    } else if (item.domain) {
      entityLabel = item.domain.charAt(0).toUpperCase() + item.domain.slice(1);
    }
    
    // Get entity ID or name
    const entityId = item.entityId || item.url?.split('/').pop() || 'unknown';
    const shortId = entityId.length > 12 ? entityId.substring(0, 12) + '...' : entityId;
    
    return `${action} ${entityLabel} (${shortId})`;
  };

  const getActionIcon = (item: any) => {
    const method = item.method?.toUpperCase();
    switch (method) {
      case 'POST':
        return <Plus className="h-4 w-4 text-green-600" />;
      case 'PUT':
      case 'PATCH':
        return <Edit className="h-4 w-4 text-blue-600" />;
      case 'DELETE':
        return <Trash2 className="h-4 w-4 text-red-600" />;
      default:
        return <RefreshCw className="h-4 w-4 text-gray-600" />;
    }
  };

  const handleRemoveItem = (item: any) => {
    if (item.source === 'auth') {
      // For auth items, completely remove from persistent queue
      console.log('[SyncQueue] Completely removing auth item from queue:', item.id);
      removeAuthSyncItem(item.id);
      // Update local state
      setSyncQueue(prev => prev.filter(i => i.id !== item.id));
    }
  };

  if (syncQueue.length === 0) {
    return (
      <div className="p-4 text-center text-sm text-muted-foreground">
        <p>All changes synced ✓</p>
      </div>
    );
  }

  return (
    <div className="max-h-96 overflow-y-auto">
      <DropdownMenuLabel className="px-2 py-1.5">Pending Sync ({syncQueue.length})</DropdownMenuLabel>
      <DropdownMenuSeparator />
      {syncQueue.map((item, index) => (
        <div key={item.id || index} className="px-2 py-2 hover:bg-accent rounded-sm text-sm group">
          <div className="flex items-center gap-2">
            {getActionIcon(item)}
            <div className="flex-1 min-w-0">
              <p className="font-medium truncate">{getActionLabel(item)}</p>
              <p className="text-xs text-muted-foreground truncate">{item.url}</p>
              {item.error && (
                <p className="text-xs text-destructive mt-1">Error: {item.error}</p>
              )}
            </div>
            <div className="flex items-center gap-1">
              {item.retries > 0 && (
                <Badge variant="outline" className="text-xs">
                  Retry {item.retries}
                </Badge>
              )}
              <Button
                variant="ghost"
                size="sm"
                className="h-6 w-6 p-0 opacity-0 group-hover:opacity-100 transition-opacity"
                onClick={() => handleRemoveItem(item)}
                title="Remove from queue"
              >
                <X className="h-3 w-3 text-destructive" />
              </Button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}


function Header({ onPosClick }: { onPosClick?: () => void }) {
  const { user, logout, business } = useAuth();
  const router = useRouter();
  const [isProfileModalOpen, setProfileModalOpen] = useState(false);
  const subscription = useLiveQuery(() => db.subscriptions.get('sub_main-business'));

  // Debug: log user object
  useEffect(() => {
    console.log('[DEBUG HEADER] User object:', user);
  }, [user]);
  
  const [branches, setBranches] = useState<Branch[]>([]);
  const [activeBranchId, setActiveBranchId] = useState<string | null>(null);
  const [activeBranch, setActiveBranch] = useState<Branch | null>(null);
  const [selectedExpiryBatch, setSelectedExpiryBatch] = useState<PurchaseRecord | null>(null);
  const [isSyncing, setIsSyncing] = useState(false);
  const [businessName, setBusinessName] = useState<string>(() => {
    if (typeof window !== 'undefined') {
      const cached = localStorage.getItem('handypos-business-name');
      return cached || 'HandyPOS';
    }
    return 'HandyPOS';
  });
  const businessNameLoadedRef = React.useRef(false);
  const { pendingCount } = useSyncStatus(activeBranchId);

  const isProPlan = subscription?.planId === 'pro' || subscription?.status === 'active' || !subscription;

  // Load business name from IndexedDB - only once
  useEffect(() => {
    if (business?.id && !businessNameLoadedRef.current) {
      businessNameLoadedRef.current = true;
      const loadBusinessName = async () => {
        const businessData = await db.business.get(business.id);
        if (businessData?.name) {
          setBusinessName(businessData.name);
          localStorage.setItem('handypos-business-name', businessData.name);
        }
      };
      loadBusinessName();
    }
  }, [business?.id]);

  // Monitor sync queue handled by useSyncStatus
  // Clear failed orders on mount
  useEffect(() => {
    clearFailedOrders();
  }, []);

  const syncHeaderBranchState = (nextBranches: Branch[], preferredBranchId?: string | null) => {
    if (typeof window === 'undefined') {
      return;
    }

    const storedActiveBranchId = localStorage.getItem(LOCAL_STORAGE_KEYS.ACTIVE_BRANCH);
    const resolvedBranchId = preferredBranchId || storedActiveBranchId || nextBranches[0]?.id || null;
    if (isWarehouseBranchId(resolvedBranchId)) {
      setBranches(nextBranches);
      setActiveBranchId(WAREHOUSE_BRANCH_ID);
      setActiveBranch(WAREHOUSE_BRANCH);
      return;
    }

    const resolvedActiveBranch =
      (resolvedBranchId
        ? nextBranches.find((branch) => String(branch.id) === String(resolvedBranchId))
        : null) ||
      nextBranches[0] ||
      null;

    setBranches(nextBranches);
    setActiveBranchId(resolvedActiveBranch?.id || resolvedBranchId || null);
    setActiveBranch(resolvedActiveBranch);
  };

  useEffect(() => {
    if (typeof window !== 'undefined') {
        const allBranches = getInitialBranches();
        syncHeaderBranchState(allBranches);

        // Listen for branches updated event
        const handleBranchesUpdated = (event: Event) => {
            const customEvent = event as CustomEvent;
            const updatedBranches = customEvent.detail?.branches;
            if (updatedBranches && Array.isArray(updatedBranches)) {
                console.log('[Header] Branches updated event received:', updatedBranches);
                syncHeaderBranchState(updatedBranches);
            }
        };

        const handleBranchChanged = (event: Event) => {
            const customEvent = event as CustomEvent;
            const nextBranchId = String(customEvent.detail?.branchId || '').trim();
            const nextBranches = getInitialBranches();
            console.log('[Header] Branch changed event received:', nextBranchId);
            syncHeaderBranchState(nextBranches, nextBranchId || undefined);
        };

        window.addEventListener('branchesUpdated', handleBranchesUpdated);
        window.addEventListener('branchChanged', handleBranchChanged);
        return () => {
          window.removeEventListener('branchesUpdated', handleBranchesUpdated);
          window.removeEventListener('branchChanged', handleBranchChanged);
        };
    }
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined' || !business?.id) {
      return;
    }

    const activeIsWarehouse = isWarehouseBranchId(activeBranchId);
    const needsBranchSync =
      branches.length === 0 ||
      (!activeIsWarehouse &&
        activeBranchId !== null &&
        !branches.some((branch) => String(branch.id) === String(activeBranchId))) ||
      (!activeIsWarehouse && !activeBranch && Boolean(activeBranchId));

    if (!needsBranchSync) {
      return;
    }

    let cancelled = false;

    const syncBranches = async () => {
      try {
        const { branches: syncedBranches, activeBranchId: syncedActiveBranchId } =
          await syncBusinessBranchesFromServer(
            business.id,
            activeBranchId || user?.branchId || undefined
          );

        if (cancelled) {
          return;
        }

        syncHeaderBranchState(syncedBranches, syncedActiveBranchId);
      } catch (error) {
        console.warn('[Header] Failed to sync branches from server:', error);
      }
    };

    void syncBranches();

    return () => {
      cancelled = true;
    };
  }, [business?.id, user?.branchId, branches, activeBranchId, activeBranch]);

  const lowStockItems = useLiveQuery(
    async () => {
      if (isWarehouseBranchId(activeBranchId)) return [];
      const branchCandidates = getBranchIdCandidates(activeBranchId);
      if (branchCandidates.length === 0) return [];

      return db.inventory
        .where('branchId')
        .anyOf(branchCandidates)
        .and((item) => {
          if (item._operation === 'delete') return false;

          const stockUnits = Number(item.stockUnits || 0);
          const reorderLevel = Number(item.reorderLevel || 0);
          const status = String(item.status || '').trim();
          const isLowByStatus = status === 'Low Stock' || status === 'Out of Stock';
          const isLowByQuantity = stockUnits <= reorderLevel;

          return isLowByStatus || isLowByQuantity;
        })
        .toArray();
    },
    [activeBranchId]
  ) || [];

  const expiringItems = useLiveQuery(
    () => {
        if (isWarehouseBranchId(activeBranchId)) return [];
        const branchCandidates = getBranchIdCandidates(activeBranchId);
        if (branchCandidates.length === 0) return [];
        const ninetyDaysFromNow = addDays(new Date(), 90).toISOString();
        return db.purchaseHistory
            .where('branchId').anyOf(branchCandidates)
            .and(item => (
              item._operation !== 'delete' &&
              !!item.expiryDate &&
              item.expiryDate <= ninetyDaysFromNow &&
              isBefore(new Date(), parseISO(item.expiryDate)) &&
              item.quantityRemaining > 0
            ))
            .toArray();
    },
    [activeBranchId]
  ) || [];

  const totalNotifications = lowStockItems.length + expiringItems.length;
  const notificationBadgeLabel = formatNotificationBadgeCount(totalNotifications);

  

  const handleSetBranch = (branch: Branch) => {
    setActiveBranchId(branch.id);
    setActiveBranch(branch);
    localStorage.setItem(LOCAL_STORAGE_KEYS.ACTIVE_BRANCH, branch.id);
    localStorage.setItem('handypos-current-branch-id', branch.id);
    window.dispatchEvent(new CustomEvent('branchChanged', { detail: { branchId: branch.id } }));
    void syncSessionSnapshotToDesktopStore();

    console.log('[Header] Branch switched to:', branch.name);
  };

  const handleLogout = () => {
    logout();
    router.push('/login');
  };
  
  if (!user) {
      return (
          <DashboardHeader>
              <div className="flex items-center gap-4">
                  <SidebarTrigger className="h-9 w-9 shrink-0" />
                  <div className="hidden lg:flex items-center gap-2">
                  <h1 className="text-xl font-semibold">HandyPOS</h1>
                  <div className="w-48 h-9 bg-muted rounded-md animate-pulse" />
                  </div>
              </div>
              <div className="flex flex-1 items-center justify-end gap-2 md:gap-4">
                  <div className="hidden w-full max-w-sm lg:block h-10 bg-muted rounded-md animate-pulse" />
              </div>
          </DashboardHeader>
      );
  }

  return (
    <>
      <DashboardHeader branchId={activeBranchId}>
        <div className="flex min-w-0 items-center gap-2 sm:gap-4">
          <SidebarTrigger className="h-9 w-9 shrink-0" />
          {user.role === 'Admin' ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  className="h-7 max-w-[44vw] gap-1 truncate px-2 text-[11px] font-medium lg:hidden"
                  title={activeBranch?.name || (isWarehouseBranchId(activeBranchId) ? 'Warehouse' : 'Select Branch')}
                >
                  <span className="min-w-0 truncate">
                    {activeBranch?.name || (isWarehouseBranchId(activeBranchId) ? 'Warehouse' : 'Select Branch')}
                  </span>
                  <ChevronDown className="h-3.5 w-3.5 shrink-0" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="max-h-[70vh] w-56 overflow-y-auto">
                <DropdownMenuLabel>Switch Branch</DropdownMenuLabel>
                <DropdownMenuItem
                  key={WAREHOUSE_BRANCH.id}
                  onSelect={() => handleSetBranch(WAREHOUSE_BRANCH)}
                  className={isWarehouseBranchId(activeBranchId) ? 'bg-accent font-medium' : ''}
                >
                  <Archive className="mr-2 h-4 w-4" />
                  Warehouse
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                {branches.length > 0 ? (
                  branches.map((branch) => (
                    <DropdownMenuItem
                      key={branch.id}
                      onSelect={() => handleSetBranch(branch)}
                      className={String(branch.id) === String(activeBranchId) ? 'bg-accent font-medium' : ''}
                    >
                      {branch.name}
                    </DropdownMenuItem>
                  ))
                ) : (
                  <DropdownMenuItem disabled>
                    No branches configured
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          ) : (
            <Badge
              variant="secondary"
              className="max-w-[42vw] truncate px-2 py-1 text-[11px] font-medium lg:hidden"
              title={activeBranch?.name || (isWarehouseBranchId(activeBranchId) ? 'Warehouse' : 'Select Branch')}
            >
              {activeBranch?.name || (isWarehouseBranchId(activeBranchId) ? 'Warehouse' : 'Select Branch')}
            </Badge>
          )}
          <div className="hidden lg:flex items-center gap-2">
            <h1 className="text-xl font-semibold">{businessName}</h1>
            {user.role === 'Admin' ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="flex items-center gap-1"
                  >
                    {activeBranch?.name || 'Select Branch'}
                    <ChevronDown className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start">
                  <DropdownMenuLabel>Switch Branch</DropdownMenuLabel>
                  <DropdownMenuItem
                    key={WAREHOUSE_BRANCH.id}
                    onSelect={() => handleSetBranch(WAREHOUSE_BRANCH)}
                    className={isWarehouseBranchId(activeBranchId) ? 'bg-accent font-medium' : ''}
                  >
                    <Archive className="mr-2 h-4 w-4" />
                    Warehouse
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  {branches.length > 0 ? (
                    branches.map((branch) => (
                      <DropdownMenuItem
                        key={branch.id}
                        onSelect={() => handleSetBranch(branch)}
                        className={String(branch.id) === String(activeBranchId) ? 'bg-accent font-medium' : ''}
                      >
                        {branch.name}
                      </DropdownMenuItem>
                    ))
                  ) : (
                    <DropdownMenuItem disabled>
                      No branches configured
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            ) : (
              <Badge variant="secondary">
                {activeBranch?.name}
              </Badge>
            )}
          </div>
        </div>

        <div className="flex flex-1 items-center justify-end gap-2 md:gap-4">
          {/* Search field - commented out
          {user.role !== 'Cashier' && (
            <div className="hidden w-full max-w-sm lg:block">
              <form>
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    className="w-full bg-background/50 pl-10"
                    placeholder="Search products, customers, orders..."
                  />
                </div>
              </form>
            </div>
          )}
          */}

          <div className="flex items-center gap-1">
            <Button size="sm" className="px-2 sm:px-3" onClick={() => onPosClick?.()}>
                <MonitorPlay className="h-4 w-4 sm:mr-2" />
                <span className="sr-only sm:not-sr-only">POS</span>
            </Button>

            {user.role !== 'Cashier' && (
              <div className="hidden sm:flex items-center gap-2">
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button 
                      variant="ghost" 
                      size="icon" 
                      className="relative"
                      title={pendingCount > 0 ? `${pendingCount} queued action${pendingCount !== 1 ? 's' : ''}` : 'Sync status'}
                    >
                      <RefreshCw className={cn("h-4 w-4", isSyncing && "animate-spin")} />
                      {pendingCount > 0 && (
                        <Badge
                          className="absolute top-1 right-1 h-4 w-4 justify-center p-0 text-[10px]"
                          variant="secondary"
                        >
                          {pendingCount}
                        </Badge>
                      )}
                      <span className="sr-only">Sync status</span>
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-80">
                    <SyncQueueDropdown branchId={activeBranchId} />
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            )}

            <Sheet>
              <SheetTrigger asChild>
                <Button variant="ghost" size="icon" className="relative">
                  <Bell />
                  {totalNotifications > 0 && (
                    <Badge
                      className="absolute top-1 right-1 flex h-4 min-w-[1rem] items-center justify-center px-1 text-[10px]"
                      variant="destructive"
                    >
                      {notificationBadgeLabel}
                    </Badge>
                  )}
                  <span className="sr-only">Notifications</span>
                </Button>
              </SheetTrigger>
              <SheetContent className="tauri-android-sidebar-safe-top flex flex-col">
                <SheetHeader>
                  <SheetTitle>Notifications & Alerts</SheetTitle>
                  <SheetDescription>
                    You have {totalNotifications} new critical alerts.
                  </SheetDescription>
                </SheetHeader>
                <div className="mt-4 flex-1 min-h-0 space-y-4 overflow-y-auto pr-1">
                  {lowStockItems.map((item) => (
                    <div key={`low-${item.id}`} className="flex items-start gap-3 rounded-lg border border-yellow-500/50 bg-yellow-500/10 p-3">
                      <div className="mt-1 flex h-5 w-5 items-center justify-center rounded-full bg-yellow-500/20 text-yellow-600">
                        <AlertTriangle className="h-3 w-3" />
                      </div>
                      <div>
                        <p className="font-semibold text-sm">
                          Low Stock: {item.name}
                        </p>
                        <p className="text-xs text-muted-foreground">
                          Only {formatInventoryQuantity(item.stockUnits)} {item.unitType} remaining. Reorder level is {formatInventoryQuantity(item.reorderLevel)}.
                        </p>
                        <Button size="xs" variant="outline" className="mt-2 text-xs h-7">
                          Create Purchase Order
                        </Button>
                      </div>
                    </div>
                  ))}
                  {expiringItems.map((item) => (
                     <div key={`exp-${item.id}`} className="flex items-start gap-3 rounded-lg border border-orange-500/50 bg-orange-500/10 p-3">
                        <div className="mt-1 flex h-5 w-5 items-center justify-center rounded-full bg-orange-500/20 text-orange-600">
                            <Pill className="h-3 w-3" />
                        </div>
                        <div>
                            <p className="font-semibold text-sm">Batch Expiring Soon</p>
                            <p className="text-xs text-muted-foreground">
                                {item.productName} (Batch: {item.batchNumber || 'N/A'}) expires on {item.expiryDate ? format(parseISO(item.expiryDate), 'PP') : 'N/A'}.
                            </p>
                             <Button
                               size="xs"
                               variant="outline"
                               className="mt-2 text-xs h-7"
                               onClick={() => setSelectedExpiryBatch(item)}
                             >
                               View Batch
                             </Button>
                        </div>
                    </div>
                  ))}
                   {totalNotifications === 0 && (
                       <div className="text-center text-muted-foreground py-10">
                           <p>No new notifications.</p>
                       </div>
                   )}
                </div>
              </SheetContent>
            </Sheet>
            <Dialog
              open={Boolean(selectedExpiryBatch)}
              onOpenChange={(open) => {
                if (!open) {
                  setSelectedExpiryBatch(null);
                }
              }}
            >
              <DialogContent className="max-h-[85vh] overflow-y-auto">
                <DialogHeader>
                  <DialogTitle>Batch Details</DialogTitle>
                  <DialogDescription>
                    Expiry alert details for the selected batch.
                  </DialogDescription>
                </DialogHeader>
                {selectedExpiryBatch && (
                  <div className="grid gap-3 text-sm">
                    <div className="flex justify-between gap-4">
                      <span className="text-muted-foreground">Product</span>
                      <span className="font-medium text-right">{selectedExpiryBatch.productName}</span>
                    </div>
                    <div className="flex justify-between gap-4">
                      <span className="text-muted-foreground">Batch No.</span>
                      <span className="font-medium text-right">{selectedExpiryBatch.batchNumber || 'N/A'}</span>
                    </div>
                    <div className="flex justify-between gap-4">
                      <span className="text-muted-foreground">Expiry Date</span>
                      <span className="font-medium text-right">
                        {selectedExpiryBatch.expiryDate
                          ? format(parseISO(selectedExpiryBatch.expiryDate), 'PP')
                          : 'N/A'}
                      </span>
                    </div>
                    <div className="flex justify-between gap-4">
                      <span className="text-muted-foreground">Quantity Remaining</span>
                      <span className="font-medium text-right">{formatInventoryQuantity(selectedExpiryBatch.quantityRemaining)}</span>
                    </div>
                    <div className="flex justify-between gap-4">
                      <span className="text-muted-foreground">Received Date</span>
                      <span className="font-medium text-right">
                        {selectedExpiryBatch.receivedDate
                          ? format(parseISO(selectedExpiryBatch.receivedDate), 'PP')
                          : 'N/A'}
                      </span>
                    </div>
                    <div className="flex justify-between gap-4">
                      <span className="text-muted-foreground">Supplier</span>
                      <span className="font-medium text-right">{selectedExpiryBatch.supplierName || 'N/A'}</span>
                    </div>
                    <div className="flex justify-between gap-4">
                      <span className="text-muted-foreground">Cost Per Unit</span>
                      <span className="font-medium text-right">{selectedExpiryBatch.costPerUnit}</span>
                    </div>
                  </div>
                )}
                <DialogFooter className="pt-4">
                  <Button asChild variant="outline">
                    <Link href="/dashboard/inventory?tab=purchases">Go to Purchases</Link>
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>

          </div>

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                className="relative h-10 w-10 rounded-full"
              >
                <Avatar className="h-9 w-9 bg-primary text-primary-foreground">
                  <AvatarFallback className="font-semibold">
                    {(user?.displayName || user?.email || 'U')
                      .split(' ')
                      .map((n) => n[0])
                      .join('')
                      .toUpperCase()
                      .slice(0, 2)}
                  </AvatarFallback>
                </Avatar>
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              <div className="flex items-center gap-3 px-2 py-3">
                <Avatar className="h-10 w-10 bg-primary text-primary-foreground">
                  <AvatarFallback className="font-semibold">
                    {(user?.displayName || user?.email || 'U')
                      .split(' ')
                      .map((n) => n[0])
                      .join('')
                      .toUpperCase()
                      .slice(0, 2)}
                  </AvatarFallback>
                </Avatar>
                <div className="flex-1 min-w-0">
                  <p className="font-semibold text-sm truncate">
                    {user?.displayName || 'User'}
                  </p>
                  <p className="text-xs text-muted-foreground truncate">
                    {user?.email}
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {user?.role}
                  </p>
                </div>
              </div>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={() => setProfileModalOpen(true)}>
                Profile
              </DropdownMenuItem>
              {user.role === 'Admin' && (
                <>
                  {/* <DropdownMenuItem asChild>
                    <Link href="/dashboard/settings/billing">Billing</Link>
                  </DropdownMenuItem> */}
                  <DropdownMenuItem asChild>
                    <Link href="/dashboard/settings">Settings</Link>
                  </DropdownMenuItem>
                </>
              )}
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={handleLogout}>Log out</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </DashboardHeader>
      {user && (
        <UserProfileModal
          user={user}
          isOpen={isProfileModalOpen}
          onOpenChange={setProfileModalOpen}
        />
      )}
    </>
  );
}


function NavGroup({ title, items, userRole, onPosClick }: { title: string, items: typeof settingsNav, userRole: User['role'], onPosClick?: () => void }) {
  const pathname = usePathname();
  const { setOpenMobile } = useSidebar();
  const { hasPermission } = useRBAC();
  
  // Restrict frontline users, but let cashiers view inventory stock/transfers.
  const isCashier = userRole === 'Cashier';
  const isWaiter = userRole === 'Waiter';
  if (isWaiter && title !== 'Point of Sale' && title !== 'Settings') {
    return null;
  }
  if (isCashier && title !== 'Point of Sale' && title !== 'Settings' && title !== 'Management') {
    return null;
  }
  
  const filteredItems = items.filter(item => hasPermission(item.permission));

  if (filteredItems.length === 0) {
    return null;
  }
  
  const [activeBranchId, setActiveBranchId] = useState<string | null>(null);
  useEffect(() => {
    if (typeof window !== 'undefined') {
        const branchId = localStorage.getItem(LOCAL_STORAGE_KEYS.ACTIVE_BRANCH);
        if(branchId) setActiveBranchId(branchId);
    }
  }, []);

  const pendingStockAudits = useLiveQuery(
    () => {
      if (!activeBranchId) return 0;
      return db.stockTakes.where({ branchId: activeBranchId, status: 'Pending Approval' }).count()
    },
    [activeBranchId]
  );

  const pendingExpenses = useLiveQuery(
    () => {
      if (!activeBranchId) return 0;
      return db.expenses.where({ branchId: activeBranchId, status: 'Pending' }).count()
    },
    [activeBranchId]
  );
  
  const totalPending = (pendingStockAudits || 0) + (pendingExpenses || 0);

  return (
    <div>
        <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider group-data-[collapsible=icon]:text-center group-data-[collapsible=icon]:[writing-mode:vertical-rl] group-data-[collapsible=icon]:mb-2">{title}</h3>
        <SidebarMenu>
        {filteredItems.map((item) => {
            const isActive = isNavItemActive(pathname, item.href);

            const handleClick = () => {
              setOpenMobile(false);
              if (item.href === '/dashboard/pos' && onPosClick) {
                onPosClick();
              }
            };

            return (
            <SidebarMenuItem key={item.href}>
                <SidebarMenuButton
                asChild={item.href !== '/dashboard/pos'}
                isActive={isActive}
                tooltip={item.label}
                aria-current={isActive ? "page" : undefined}
                onClick={handleClick}
                >
                {item.href === '/dashboard/pos' ? (
                  <button className="flex justify-between items-center w-full">
                    <div className="flex items-center gap-3">
                        <item.icon />
                        <span className="group-data-[collapsible=icon]:hidden">{item.label}</span>
                    </div>
                  </button>
                ) : (
                  <Link href={item.href} className="flex justify-between items-center w-full">
                    <div className="flex items-center gap-3">
                        <item.icon />
                        <span className="group-data-[collapsible=icon]:hidden">{item.label}</span>
                    </div>
                    {item.href === '/dashboard/approvals' && totalPending > 0 && (
                        <Badge className="h-5 group-data-[collapsible=icon]:hidden">{totalPending}</Badge>
                    )}
                  </Link>
                )}
                </SidebarMenuButton>
            </SidebarMenuItem>
            );
        })}
        </SidebarMenu>
    </div>
  )
}

function AppSidebar({ user, onPosClick }: { user: User, onPosClick?: () => void }) {
  const pathname = usePathname();
  const { hasPermission } = useRBAC();
  const { setOpenMobile } = useSidebar();

  const filteredItems = navItems.filter(item => hasPermission(item.permission));
  const filteredSettingsItems = user.role === 'Admin'
    ? settingsNav.filter(item => hasPermission(item.permission))
    : [];

  return (
    <>
      <SidebarHeader className="p-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 group-data-[collapsible=icon]:hidden">
            <HandyPosLogo className="size-7" />
            <span className="text-lg font-semibold tracking-tight">HandyPOS</span>
          </div>
        </div>
      </SidebarHeader>

      <SidebarContent className="flex-1 px-2 py-1.5">
        <SidebarMenu>
          {filteredItems.map((item) => {
            const isActive = isNavItemActive(pathname, item.href);

            const handleClick = () => {
              setOpenMobile(false);
              if (item.href === '/dashboard/pos' && onPosClick) {
                onPosClick();
              }
            };

            return (
              <SidebarMenuItem key={item.href}>
                <SidebarMenuButton
                  asChild={item.href !== '/dashboard/pos'}
                  isActive={isActive}
                  tooltip={item.label}
                  aria-current={isActive ? "page" : undefined}
                  onClick={handleClick}
                >
                  {item.href === '/dashboard/pos' ? (
                    <button className="flex items-center gap-3">
                      <item.icon />
                      <span className="group-data-[collapsible=icon]:hidden">{item.label}</span>
                    </button>
                  ) : (
                    <Link href={item.href} className="flex items-center gap-3">
                      <item.icon />
                      <span className="group-data-[collapsible=icon]:hidden">{item.label}</span>
                    </Link>
                  )}
                </SidebarMenuButton>
              </SidebarMenuItem>
            );
          })}
        </SidebarMenu>
      </SidebarContent>

      <SidebarFooter className="p-2 pt-1">
        <SidebarMenu>
          {filteredSettingsItems.map((item) => {
            const isActive = isNavItemActive(pathname, item.href);
            const handleClick = () => {
              setOpenMobile(false);
            };

            return (
              <SidebarMenuItem key={item.href}>
                <SidebarMenuButton
                  asChild
                  isActive={isActive}
                  tooltip={item.label}
                  aria-current={isActive ? "page" : undefined}
                  onClick={handleClick}
                >
                  <Link href={item.href} className="flex items-center gap-3">
                    <item.icon />
                    <span className="group-data-[collapsible=icon]:hidden">{item.label}</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
            );
          })}
        </SidebarMenu>
      </SidebarFooter>
    </>
  );
}

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, loading, logout, business } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const { toast } = useToast();
  const [isPosModalOpen, setIsPosModalOpen] = useState(false);
  const [isEisActivationDialogOpen, setIsEisActivationDialogOpen] = useState(false);
  const [activeBranchId, setActiveBranchId] = useState<string | null>(null);
  const [eisActivationGate, setEisActivationGate] = useState<EisActivationGateState>({
    checking: false,
    required: false,
    reason: '',
  });
  const [activationGateRefreshKey, setActivationGateRefreshKey] = useState(0);
  const startupConfigEnsureRef = React.useRef<Record<string, number>>({});
  const activationGateToastRef = React.useRef('');
  const isEisActivationPath = pathname.startsWith(EIS_ACTIVATION_PATH);

  const forceEisActivation = useCallback((reason?: string) => {
    const message = reason || eisActivationGate.reason || 'Activate this device first.';
    const toastKey = `${activeBranchId || ''}:${message}`;
    if (activationGateToastRef.current !== toastKey) {
      activationGateToastRef.current = toastKey;
      toast({
        variant: 'destructive',
        title: 'Activation required',
        description: message,
      });
    }
    setIsPosModalOpen(false);
    setIsEisActivationDialogOpen(true);
  }, [activeBranchId, eisActivationGate.reason, toast]);

  const handleOpenPos = useCallback(() => {
    if (isWarehouseBranchId(activeBranchId)) {
      toast({
        variant: 'destructive',
        title: 'Warehouse selected',
        description: 'Switch to a branch to make sales.',
      });
      return;
    }
    if (eisActivationGate.required) {
      forceEisActivation();
      return;
    }
    setIsPosModalOpen(true);
  }, [activeBranchId, eisActivationGate.required, forceEisActivation, toast]);

  const handlePosModalOpenChange = useCallback((open: boolean) => {
    if (open && isWarehouseBranchId(activeBranchId)) {
      toast({
        variant: 'destructive',
        title: 'Warehouse selected',
        description: 'Switch to a branch to make sales.',
      });
      return;
    }
    if (open && eisActivationGate.required) {
      forceEisActivation();
      return;
    }
    setIsPosModalOpen(open);
  }, [activeBranchId, eisActivationGate.required, forceEisActivation, toast]);

  useEffect(() => {
    if (isPosModalOpen && eisActivationGate.required) {
      forceEisActivation(eisActivationGate.reason);
    }
  }, [eisActivationGate.required, eisActivationGate.reason, forceEisActivation, isPosModalOpen]);

  useEffect(() => {
    if (loading) return;

    const rawTokens =
      localStorage.getItem(LOCAL_STORAGE_KEYS.AUTH_TOKENS) ??
      localStorage.getItem(LOCAL_STORAGE_KEYS.LEGACY_AUTH_TOKENS);

    let hasValidTokens = false;
    if (rawTokens) {
      try {
        const parsedTokens = JSON.parse(rawTokens);
        hasValidTokens = Boolean(parsedTokens?.access && parsedTokens?.refresh);
      } catch {
        hasValidTokens = false;
      }
    }

    if (!hasValidTokens) {
      if (user) {
        logout();
      }
      router.replace('/login');
      return;
    }

    // Tokens are present; allow auth bootstrap to finish restoring the user model
    // before treating the session as invalid.
    if (!user) {
      return;
    }
  }, [user, loading, logout, router]);

  useEffect(() => {
    if (user && user.branchId) {
        if (typeof window !== 'undefined') {
            const storedActiveBranch = localStorage.getItem(LOCAL_STORAGE_KEYS.ACTIVE_BRANCH);
            if (
              user.role !== 'Admin' &&
              !isWarehouseBranchId(storedActiveBranch) &&
              storedActiveBranch !== user.branchId
            ) {
                localStorage.setItem(LOCAL_STORAGE_KEYS.ACTIVE_BRANCH, user.branchId);
                window.location.reload();
            }
        }
    }
  }, [user]);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const syncActiveBranch = (nextBranchId?: string | null) => {
      const branchId = nextBranchId ?? localStorage.getItem(LOCAL_STORAGE_KEYS.ACTIVE_BRANCH);
      setActiveBranchId(branchId ? String(branchId) : null);
    };

    const handleBranchChanged = (event: Event) => {
      const customEvent = event as CustomEvent;
      const nextBranchId = customEvent.detail?.branchId;
      syncActiveBranch(nextBranchId ? String(nextBranchId) : null);
    };

    const handleBranchesUpdated = () => syncActiveBranch();

    syncActiveBranch();
    window.addEventListener('branchChanged', handleBranchChanged);
    window.addEventListener('branchesUpdated', handleBranchesUpdated);

    return () => {
      window.removeEventListener('branchChanged', handleBranchChanged);
      window.removeEventListener('branchesUpdated', handleBranchesUpdated);
    };
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const handleActivationRequested = (event: Event) => {
      const customEvent = event as CustomEvent<{ reason?: string }>;
      const reason = customEvent.detail?.reason || 'Activate this device first.';
      setEisActivationGate((previous) => ({
        ...previous,
        required: true,
        reason,
      }));
      forceEisActivation(reason);
    };

    window.addEventListener(EIS_TERMINAL_ACTIVATION_REQUESTED_EVENT, handleActivationRequested);
    return () => {
      window.removeEventListener(EIS_TERMINAL_ACTIVATION_REQUESTED_EVENT, handleActivationRequested);
    };
  }, [forceEisActivation]);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const refreshActivationGate = () => {
      setActivationGateRefreshKey((value) => value + 1);
    };

    void ensureTauriDeviceIdentity().finally(refreshActivationGate);
    window.addEventListener(EIS_TERMINAL_ACTIVATION_CHANGED_EVENT, refreshActivationGate);
    window.addEventListener(DEVICE_IDENTITY_CHANGED_EVENT, refreshActivationGate);
    return () => {
      window.removeEventListener(EIS_TERMINAL_ACTIVATION_CHANGED_EVENT, refreshActivationGate);
      window.removeEventListener(DEVICE_IDENTITY_CHANGED_EVENT, refreshActivationGate);
    };
  }, []);

  useEffect(() => {
    if (loading || !user || !business?.id || !activeBranchId || isWarehouseBranchId(activeBranchId)) {
      setEisActivationGate({ checking: false, required: false, reason: '' });
      return;
    }

    const businessId = String(business.id).trim();
    const normalizedBranchId = normalizeApiBranchId(activeBranchId);
    if (!businessId || !normalizedBranchId) {
      setEisActivationGate({ checking: false, required: false, reason: '' });
      return;
    }

    let cancelled = false;

    const failGate = (reason: string) => {
      if (cancelled) return;
      setEisActivationGate({ checking: false, required: true, reason });
    };

    const passGate = () => {
      if (cancelled) return;
      setEisActivationGate({ checking: false, required: false, reason: '' });
    };

    const verifyDeviceTerminalActivation = async () => {
      setEisActivationGate((previous) => ({
        ...previous,
        checking: true,
      }));

      let eisEnabled = resolveCachedEisEnabled(businessId, business);
      try {
        const backendBusiness = await authFetch.fetch<any>(`/business/businesses/${businessId}/`);
        const backendEnabled = readBooleanFlag(
          backendBusiness?.enable_eis ??
          backendBusiness?.enableEis ??
          backendBusiness?.eis_enabled ??
          backendBusiness?.eisEnabled
        );
        if (backendEnabled !== null) {
          eisEnabled = backendEnabled;
        }
      } catch (error) {
        console.warn('[Dashboard] Could not confirm backend EIS status for terminal activation gate:', error);
      }

      if (!eisEnabled) {
        passGate();
        return;
      }

      const currentDeviceSerial = getDeviceSerial();
      if (!currentDeviceSerial) {
        failGate('Restart the app.');
        return;
      }
      const cachedActivatedTerminal = readCachedActivatedTerminal(
        businessId,
        activeBranchId,
        currentDeviceSerial
      );

      try {
        const terminalsResponse = await authFetch.fetch<any>('/mra-eis/terminals/');
        const terminals = extractApiList<any>(terminalsResponse);
        const matchingTerminal = terminals.find((terminal) => {
          const sameBranch = normalizeApiBranchId(getApiBranchId(terminal)) === normalizedBranchId;
          const isActive = String(terminal?.status || '').toLowerCase() === 'active';
          const terminalDeviceSerial = getApiDeviceSerial(terminal);
          return (
            sameBranch &&
            isActive &&
            terminalDeviceSerial.toLowerCase() === currentDeviceSerial.toLowerCase()
          );
        });

        if (matchingTerminal) {
          persistCachedTerminal(businessId, activeBranchId, matchingTerminal);
          passGate();
          return;
        }

        failGate('Activate this device first.');
      } catch (error: any) {
        console.error('[Dashboard] Failed to verify EIS terminal activation:', error);
        if (cachedActivatedTerminal) {
          console.warn('[Dashboard] Using cached EIS terminal activation because backend verification is unavailable.');
          passGate();
          return;
        }
        failGate('Activation check failed.');
      }
    };

    void verifyDeviceTerminalActivation();

    return () => {
      cancelled = true;
    };
  }, [activeBranchId, activationGateRefreshKey, business, business?.id, loading, user]);

  useEffect(() => {
    if (loading || !user || !business?.id || !activeBranchId || isWarehouseBranchId(activeBranchId)) {
      return;
    }

    const normalizedBranchId = normalizeApiBranchId(activeBranchId);
    const businessId = String(business.id).trim();
    if (!normalizedBranchId || !businessId) {
      return;
    }

    const today = new Date().toISOString().slice(0, 10);
    const sessionKey = `handypos-mra-config-startup-check:${businessId}:${normalizedBranchId}:${today}`;
    if (sessionStorage.getItem(sessionKey)) {
      return;
    }

    const refKey = `${businessId}:${normalizedBranchId}`;
    const now = Date.now();
    const lastAttemptAt = startupConfigEnsureRef.current[refKey] || 0;
    if (now - lastAttemptAt < 5 * 60 * 1000) {
      return;
    }
    startupConfigEnsureRef.current[refKey] = now;

    let cancelled = false;

    const ensureFreshConfigAtStartup = async () => {
      if (typeof navigator !== 'undefined' && navigator.onLine === false) {
        console.log('[Dashboard] Offline - skipping startup MRA config check.');
        return;
      }

      try {
        let eisEnabled = resolveCachedEisEnabled(businessId, business);
        try {
          const backendBusiness = await authFetch.fetch<any>(`/business/businesses/${businessId}/`);
          const backendEnabled = readBooleanFlag(
            backendBusiness?.enable_eis ??
            backendBusiness?.enableEis ??
            backendBusiness?.eis_enabled ??
            backendBusiness?.eisEnabled
          );
          if (backendEnabled !== null) {
            eisEnabled = backendEnabled;
          }
        } catch (businessError) {
          console.warn('[Dashboard] Could not confirm backend EIS status for startup config check:', businessError);
        }

        if (!eisEnabled) {
          return;
        }

        let terminalId = '';
        try {
          const terminalsResponse = await authFetch.fetch<any>('/mra-eis/terminals/');
          const terminals = extractApiList<any>(terminalsResponse);
          const matchingTerminal = terminals.find(
            (item) => normalizeApiBranchId(getApiBranchId(item)) === normalizedBranchId
          );
          terminalId = String(matchingTerminal?.id || '').trim();
        } catch (terminalError) {
          console.warn('[Dashboard] Could not resolve MRA terminal for startup config check:', terminalError);
        }

        const params = new URLSearchParams({ business_id: businessId });
        if (terminalId) {
          params.set('terminal_id', terminalId);
        }

        const response = await authFetch.fetch<any>(
          `/mra-eis/configurations/ensure_fresh/?${params.toString()}`,
          {
            method: 'POST',
            body: JSON.stringify({ require_success: false, startup_check: true }),
          }
        );

        if (cancelled) {
          return;
        }

        sessionStorage.setItem(sessionKey, new Date().toISOString());

        if (response?.fresh === false && response?.error) {
          toast({
            variant: 'destructive',
            title: 'Config refresh failed',
            description: 'Try again.',
          });
        } else if (response?.refreshed) {
          toast({
            title: 'New MRA configs downloaded',
            description: 'Latest EIS configs saved.',
          });
        }
      } catch (error: any) {
        if (cancelled) {
          return;
        }
        console.error('[Dashboard] Startup MRA config check failed:', error);
        toast({
          variant: 'destructive',
          title: 'Config refresh failed',
          description: 'Try again.',
        });
      }
    };

    void ensureFreshConfigAtStartup();

    return () => {
      cancelled = true;
    };
  }, [activeBranchId, business, business?.id, loading, toast, user]);

  useEffect(() => {
    if (user) {
        const accessibleRoutes = [...navItems, ...settingsNav]
            .filter(item => checkPermission(user.role, item.permission))
            .map(item => item.href);

        if (isEisActivationPath) {
            if (!checkPermission(user.role, 'manage_settings')) {
                const roleStr = (user.role ?? 'Admin').toLowerCase();
                router.replace(roleStr === 'cashier' || roleStr === 'waiter' ? '/dashboard/pos' : '/dashboard');
            }
            return;
        }
        
        // Allow access to the base dashboard page
        if (pathname === '/dashboard') return;

        // Only enforce redirects when we have a non-empty set of accessible routes
        if (accessibleRoutes.length > 0 && !accessibleRoutes.some(route => pathname.startsWith(route))) {
            // If current route is not accessible, redirect to a default page for that role
            const roleStr = (user.role ?? 'Admin').toLowerCase();
            if (roleStr === 'cashier' || roleStr === 'waiter') {
                router.replace('/dashboard/pos');
            } else {
                router.replace('/dashboard');
            }
        }
    }
  }, [isEisActivationPath, user, pathname, router]);

  const handleTerminalActivated = useCallback((terminalResponse: any) => {
    const terminal = terminalResponse?.terminal && typeof terminalResponse.terminal === 'object'
      ? { ...terminalResponse.terminal, ...terminalResponse }
      : terminalResponse;

    if (business?.id && activeBranchId && terminal) {
      persistCachedTerminal(String(business.id), activeBranchId, terminal);
    }

    const isActive = String(terminal?.status || '').toLowerCase() === 'active';
    if (isActive) {
      setEisActivationGate({ checking: false, required: false, reason: '' });
    }
    setActivationGateRefreshKey((value) => value + 1);
  }, [activeBranchId, business?.id]);

  if (loading || !user) {
    return (
        <div className="flex h-screen h-[100dvh] items-center justify-center">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
        </div>
    );
  }

  return (
    <SidebarProvider>
      <div className="flex h-screen h-[100dvh] w-full">
        <Sidebar className="hidden lg:flex lg:flex-col">
           <AppSidebar user={user} onPosClick={handleOpenPos} />
        </Sidebar>
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          <Header onPosClick={handleOpenPos} />
          <main className="tauri-android-safe-bottom min-h-0 w-full flex-1 bg-background/95">
            <div className="mx-auto flex h-full w-full max-w-[1540px] flex-col px-4 py-4 sm:px-6 lg:px-8 xl:py-6 2xl:px-10">
              {eisActivationGate.required && (
                <div className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4 text-amber-950 dark:text-amber-200">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex min-w-0 items-start gap-3">
                      <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0" />
                      <div className="min-w-0">
                        <p className="text-sm font-semibold">Activation required</p>
                        <p className="mt-1 text-sm">
                          {eisActivationGate.reason || 'Activate this device first.'}
                        </p>
                      </div>
                    </div>
                    <Button
                      type="button"
                      className="w-full sm:w-auto"
                      onClick={() => setIsEisActivationDialogOpen(true)}
                    >
                      <Zap className="mr-2 h-4 w-4" />
                      Activate Now
                    </Button>
                  </div>
                </div>
              )}
              {children}
            </div>
          </main>
        </div>
      </div>
      {activeBranchId && !isWarehouseBranchId(activeBranchId) && (
        <PosModal
          branchId={activeBranchId}
          isOpen={isPosModalOpen}
          onOpenChange={handlePosModalOpenChange}
        />
      )}
      <TerminalActivationDialog
        open={isEisActivationDialogOpen}
        onOpenChange={setIsEisActivationDialogOpen}
        businessId={business?.id}
        branchId={activeBranchId}
        reason={eisActivationGate.reason}
        onActivated={handleTerminalActivated}
      />
      <ThemeCustomizer />
    </SidebarProvider>
  );
}
