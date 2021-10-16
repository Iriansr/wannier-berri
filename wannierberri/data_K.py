#                                                            #
# This file is distributed as part of the WannierBerri code  #
# under the terms of the GNU General Public License. See the #
# file `LICENSE' in the root directory of the WannierBerri   #
# distribution, or http://www.gnu.org/copyleft/gpl.txt       #
#                                                            #
# The WannierBerri code is hosted on GitHub:                 #
# https://github.com/stepan-tsirkin/wannier-berri            #
#                     written by                             #
#           Stepan Tsirkin, University of Zurich             #
#                                                            #
#------------------------------------------------------------


## TODO : maybe to make some lazy_property's not so lazy to save some memory
import numpy as np
import lazy_property
from .__parallel import pool
from collections import defaultdict
from .__system import System
import time
from .__utility import  print_my_name_start,print_my_name_end, FFT_R_to_k, alpha_A,beta_A
import gc
import os
from .__tetrahedron import TetraWeights,get_bands_in_range,get_bands_below_range
from .__formula_3 import Matrix_ln, Matrix_GenDer_ln

def _rotate_matrix(X):
    return X[1].T.conj().dot(X[0]).dot(X[1])


def parity_I(name,der=0):
    """returns True if quantity is odd under inversion,(after a real trace is taken, if appropriate)
     False otherwise  
    raises for unknown quantities"""
    if name in ['Ham','CC','FF','OO','SS']:  # even before derivative
        p=0
    elif name in ['T_wcc']:     # odd before derivative
        p=1
    elif name in ['D','AA','BB','CCab']:
        return None
    else :
        raise ValueError(f"parity under inversion unknown for {name}")
    return bool( (p+der)%2 )


def parity_TR(name,der=0):
    """returns True if quantity is odd under TR, ,(after a real trace is taken, if appropriate)
    False otherwise
    raises ValueError for unknown quantities"""
    if name in ['Ham','T_wcc']:   # even before derivative
        p=0 
    elif name in ['CC','FF','OO']:      # odd before derivative
        p=1
    elif name in ['D','AA','BB','CCab']:
        return None
    else :
        raise ValueError(f"parity under TR unknown for {name}")
    return bool( (p+der)%2 )

class _Dcov(Matrix_ln):
    def __init__(self,data_K):
        super().__init__(data_K.D_H)
    def nn(self,ik,inn,out):
        raise ValueError("Dln should not be called within inner states")


   
class Data_K(System):
    default_parameters =  {
                    'frozen_max': -np.Inf,
                    'delta_fz':0.1,
                    'Emin': -np.Inf ,
                    'Emax': np.Inf ,
                    'use_wcc_phase':False,
                    'fftlib' : 'fftw',
                    'npar_k' : 1 ,
                    'random_gauge':False,
                    'degen_thresh_random_gauge':1e-4 ,
                    '_FF_antisym'   : False,
                    '_CCab_antisym' : False
                       }

    __doc__ = """
    class to store data of the FFT grid. Is destroyed after  everything is evaluated for the FFT grid

    Parameters
    -----------
    frozen_max : float
        position of the upper edge of the frozen window. Used in the evaluation of orbital moment. But not necessary. 
        If not specified, attempts to read this value from system. Othewise set to  ``{frozen_max}``
    random_gauge : bool
        applies random unitary rotations to degenerate states. Needed only for testing, to make sure that gauge covariance is preserved. Default: ``{random_gauge}``
    degen_thresh_random_gauge : float
        threshold to consider bands as degenerate for random_gauge Default: ``{degen_thresh_random_gauge}``
    delta_fz:float
        size of smearing for B matrix with frozen window, from frozen_max-delta_fz to frozen_max. Default: ``{delta_fz}``
    """ .format(**default_parameters)



    def __init__(self,system,dK,grid,Kpoint=None,**parameters):
#        self.spinors=system.spinors
        self.system=system
        self.set_parameters(**parameters)

        self.grid=grid
        self.NKFFT=grid.FFT
        self.select_K=np.ones(self.NKFFT_tot,dtype=bool)
        self.findif=grid.findif
        self.real_lattice = system.real_lattice
        self.num_wann=self.system.num_wann
        self.Kpoint=Kpoint
        self.nkptot = self.NKFFT[0]*self.NKFFT[1]*self.NKFFT[2]
        #self.wannier_centers_cart_wcc_phase=system.wannier_centers_wcc_phase
#        print (f"wannier centers in Data_K : \n{self.system.wannier_centers_cart_wcc_phase}\n")
        self.cRvec_wcc=self.system.cRvec_p_wcc

        self.fft_R_to_k=FFT_R_to_k(self.system.iRvec,self.NKFFT,self.num_wann, numthreads=self.npar_k if self.npar_k>0 else 1,lib=self.fftlib)
        self.poolmap=pool(self.npar_k)[0]

        self.expdK=np.exp(2j*np.pi*self.system.iRvec.dot(dK))
        self.dK=dK
        self._bar_quantities = {}
        self._covariant_quantities = {}

    def set_parameters(self,**parameters):
        for param in self.default_parameters:
            if param in parameters:
                vars(self)[param]=parameters[param]
            else: 
                vars(self)[param]=self.default_parameters[param]
        if 'frozen_max' not in parameters:
            try : 
                self.frozen_max= self.system.frozen_max
            except:
                pass 



###########################################
###   Now the **_R objects are evaluated only on demand 
### - as Lazy_property (if used more than once) 
###   as property   - iif used only once
###   let's write them explicitly, for better code readability
###########################

    @lazy_property.LazyProperty
    def Ham_R(self):
        return self.system.Ham_R*self.expdK[None,None,:]

    @lazy_property.LazyProperty
    def AA_R(self):
        return self.system.AA_R*self.expdK[None,None,:,None]

    #  this is a bit ovberhead, but to maintain uniformity of the code let's use this
    @lazy_property.LazyProperty
    def T_wcc_R(self):
        nw = self.num_wann
        res = np.zeros((nw,nw,self.system.nRvec,3),dtype=complex)
        res [np.arange(nw),np.arange(nw),self.system.iR0,:] = self.system.wannier_centers_cart_wcc_phase
        return res


    @lazy_property.LazyProperty
    def OO_R(self):
        # We do not multiply by expdK, because it is already accounted in AA_R
        return  1j*(self.cRvec_wcc[:,:,:,alpha_A]* self.AA_R[:,:,:,beta_A] - self.cRvec_wcc[:,:,:,beta_A]* self.AA_R[:,:,:,alpha_A])

    @lazy_property.LazyProperty
    def BB_R(self):
        return self.system.BB_R*self.expdK[None,None,:,None]

    @lazy_property.LazyProperty
    def CC_R(self):
        return self.system.CC_R*self.expdK[None,None,:,None]

    @lazy_property.LazyProperty
    def CCab_R(self):
        if self._CCab_antisym:
            CCab = np.zeros( (self.num_wann,self.num_wann,self.system.nRvec,3,3),dtype = complex )
            CCab[:,:,:,alpha_A,beta_A] =  -0.5j*self.CC_R
            CCab[:,:,:,beta_A,alpha_A] =   0.5j*self.CC_R
            return CCab
        else :
            return self.system.CCab_R*self.expdK[None,None,:,None,None]

    @lazy_property.LazyProperty
    def FF_R(self):
        if self._FF_antisym:
            return self.cRvec_wcc[:,:,:,:,None]* self.AA_R[:,:,:,None,:]
        else :
            return self.system.FF_R*self.expdK[None,None,:,None,None]

    @lazy_property.LazyProperty
    def SS_R(self):
        return self.system.SS_R*self.expdK[None,None,:,None]

    @property
    def SH_R(self):
        return self.system.SH_R*self.expdK[None,None,:,None]

    @property
    def SR_R(self):
        return self.system.SR_R*self.expdK[None,None,:,None,None]

    @property
    def SA_R(self):
        return self.system.SA_R*self.expdK[None,None,:,None,None]

    @property
    def SHA_R(self):
        return self.system.SHA_R*self.expdK[None,None,:,None,None]

    @property
    def SHR_R(self):
        return self.system.SHR_R*self.expdK[None,None,:,None,None]

###############################################################

###########
#  TOOLS  #
###########

    def _rotate(self,mat):
        print_my_name_start()
        assert mat.ndim>2
        if mat.ndim==3:
            return  np.array(self.poolmap( _rotate_matrix , zip(mat,self.UU_K)))
        else:
            for i in range(mat.shape[-1]):
                mat[...,i]=self._rotate(mat[...,i])
            return mat


    def _R_to_k_H(self,XX_R,der=0,hermitean=True):
        """ converts from real-space matrix elements in Wannier gauge to 
            k-space quantities in k-space. 
            der [=0] - defines the order of comma-derivative 
            hermitean [=True] - consider the matrix hermitean
            WARNING: the input matrix is destroyed, use np.copy to preserve it"""
        
        for i in range(der):
            shape_cR = np.shape(self.cRvec_wcc)
            XX_R=1j*XX_R.reshape( (XX_R.shape)+(1,) ) * self.cRvec_wcc.reshape((shape_cR[0],shape_cR[1],self.system.nRvec)+(1,)*len(XX_R.shape[3:])+(3,))
        return  self._rotate((self.fft_R_to_k( XX_R,hermitean=hermitean))[self.select_K]  )

#####################
#  Basic variables  #
#####################
    @lazy_property.LazyProperty
    def nbands(self):
        return self.Ham_R.shape[0]

    @lazy_property.LazyProperty
    def kpoints_all(self):
        return (self.grid.points_FFT+self.dK[None])%1

    @lazy_property.LazyProperty
    def NKFFT_tot(self):
        return np.prod(self.NKFFT)

    @lazy_property.LazyProperty
    def tetraWeights(self):
        return TetraWeights(self.E_K,self.E_K_corners)

    def get_bands_in_range(self,emin,emax,op=0,ed=None):
        if ed is None: ed=self.NKFFT_tot
        select = [ np.where((self.E_K[ik]>=emin)*(self.E_K[ik]<=emax))[0] for ik in range(op,ed) ]
        return  [ {ib:self.E_K[ik+op,ib]  for ib in sel } for ik,sel in enumerate(select) ]

    def get_bands_below_range(self,emin,emax,op=0,ed=None):
        if ed is None: ed=self.NKFFT_tot
        res=[np.where((self.E_K[ik]<emin))[0] for ik in range(op,ed)]
        return [{a.max():self.E_K[ik+op,a.max()]} if len(a)>0 else [] for ik,a in enumerate(res)]

    def get_bands_in_range_sea(self,emin,emax,op=0,ed=None):
        if ed is None: ed=self.NKFFT_tot
        res=self.get_bands_in_range(emin,emax,op,ed)
        for ik in range(op,ed):
           add=np.where((self.E_K[ik]<emin))[0]
#           print ("add : ",add," / ",self.E_K[ik])
           if len(add)>0:
               res[ik-op][add.max()]=self.E_K[ik,add.max()]
        return res


    def get_bands_in_range_groups(self,emin,emax,op=0,ed=None,degen_thresh=-1,sea=False):
#        get_bands_in_range(emin,emax,Eband,degen_thresh=-1,Ebandmin=None,Ebandmax=None)
        if ed is None: ed=self.NKFFT_tot
        res=[]
        for ik in range(op,ed):
            bands_in_range=get_bands_in_range(emin,emax,self.E_K[ik],degen_thresh=degen_thresh)
            weights= { (ib1,ib2):self.E_K[ik,ib1:ib2].mean() 
                          for ib1,ib2 in bands_in_range  
                     }
            if sea :
                bandmax=get_bands_below_range(emin,self.E_K[ik])
#                print ("bandmax=",bandmax)
                if len(bands_in_range)>0 :
                    bandmax=min(bandmax, bands_in_range[0][0])
#                print ("now : bandmax=",bandmax ,self.E_K[ik][bandmax] )
                if bandmax>0:
                    weights[(0,bandmax)]=-np.Inf
            res.append( weights )
        return res

###################################################
#  Basic variables and their standard derivatives #
###################################################
    @lazy_property.LazyProperty
    def E_K(self):
        print_my_name_start()
        EUU=self.poolmap(np.linalg.eigh,self.HH_K)
        E_K=np.array([euu[0] for euu in EUU])
        select=(E_K>self.Emin)*(E_K<self.Emax)
        self.select_K=np.all(select,axis=1)
        self.select_B=np.all(select,axis=0)
        self.nk_selected=self.select_K.sum()
        self.nb_selected=self.select_B.sum()
        self._UU=np.array([euu[1] for euu in EUU])[self.select_K,:][:,self.select_B]
        print_my_name_end()
        return E_K[self.select_K,:][:,self.select_B]
    
    # evaluate the energies in the corners of the parallelepiped, in order to use tetrahedron method
    @lazy_property.LazyProperty
    def E_K_corners(self):
        dK2=self.Kpoint.dK_fullBZ/2
        expdK=np.exp(2j*np.pi*self.system.iRvec*dK2[None,:])  # we omit the wcc phases here, because they do not affect hte energies
        expdK=np.array([1./expdK,expdK])
        Ecorners=np.zeros((self.nk_selected,2,2,2,self.nb_selected),dtype=float)
        for ix in 0,1:
            for iy in 0,1:
               for iz in 0,1:
                   _expdK=expdK[ix,:,0]*expdK[iy,:,1]*expdK[iz,:,2]
                   _Ham_R=self.Ham_R[:,:,:]*_expdK[None,None,:]
                   _HH_K=self.fft_R_to_k( _Ham_R, hermitean=True)
                   E=np.array(self.poolmap(np.linalg.eigvalsh,_HH_K))
                   Ecorners[:,ix,iy,iz,:]=E[self.select_K,:][:,self.select_B]
        print_my_name_end()
        return Ecorners

    @property
    def HH_K(self):
        return self.fft_R_to_k( self.Ham_R, hermitean=True) 

    @lazy_property.LazyProperty
    def delE_K(self):
        print_my_name_start()
        delE_K = np.einsum("klla->kla",self.Xbar('Ham',1))
        check=np.abs(delE_K).imag.max()
        if check>1e-10: raiseruntimeError ( "The band derivatives have considerable imaginary part: {0}".format(check) )
        return delE_K.real

    def Xbar(self,name,der=0):
        key = (name,der)
        if key not in self._bar_quantities:
            self._bar_quantities[key] = self._R_to_k_H( getattr(self,name+'_R').copy() , der=der, 
                    hermitean = (name in ['AA','SS','OO'])  )
        return self._bar_quantities[key]

    def covariant(self,name,commader=0,gender=0,save = True):
        assert commader*gender==0 , "cannot mix comm and generalized derivatives"
        key = (name,commader,gender)
        if key not in self._covariant_quantities:
            if gender == 0:
                res = Matrix_ln(self.Xbar(name,commader) ,
                    Iodd = parity_I(name,commader),TRodd = parity_TR(name,commader)
                        )
            elif gender == 1:
                if name == 'Ham':
                    res =  self.V_covariant
                else:
                    res = Matrix_GenDer_ln(self.covariant(name),self.covariant(name,commader=1),
                        self.Dcov ,
                    Iodd = parity_I(name,gender),TRodd = parity_TR(name,gender) 
                            )
            else:
                raise NotImplementedError()
            if not save: 
                return res
            else:
                self._covariant_quantities[key] = res 
        return self._covariant_quantities[key]

    @property
    def V_covariant(self):
        class V(Matrix_ln):
            def __init__(self,matrix):
                super().__init__(matrix,TRodd = True, Iodd = True)

            def ln(self,ik,inn,out):
                return np.zeros((len(out),len(inn),3),dtype=complex)
        return V(self.Xbar('Ham',der=1))


    @lazy_property.LazyProperty
    def Dcov(self):
        return  _Dcov(self)



    @lazy_property.LazyProperty
    def dEig_inv(self):
        dEig_threshold=1.e-7
        dEig=self.E_K[:,:,None]-self.E_K[:,None,:]
        select=abs(dEig)<dEig_threshold
        dEig[select]=dEig_threshold
        dEig=1./dEig
        dEig[select]=0.
        return dEig

#    defining sets of degenerate states - needed only for testing with random_gauge
    @lazy_property.LazyProperty
    def degen(self):
            A=[np.where(E[1:]-E[:-1]>self.degen_thresh_random_gauge)[0]+1 for E in self.E_K ]
            A=[ [0,]+list(a)+[len(E)] for a,E in zip(A,self.E_K) ]
            return [[(ib1,ib2) for ib1,ib2 in zip(a,a[1:]) if ib2-ib1>1 ]    for a in A ]

    @lazy_property.LazyProperty
    def UU_K(self):
        print_my_name_start()
        self.E_K
        # the following is needed only for testing : 
        if self.random_gauge:
            from scipy.stats import unitary_group
            cnt=0
            s=0
            for ik,deg in enumerate(self.true):
                for ib1,ib2 in deg:
                    self._UU[ik,:,ib1:ib2]=self._UU[ik,:,ib1:ib2].dot( unitary_group.rvs(ib2-ib1) )
                    cnt+=1
                    s+=ib2-ib1
#            print ("applied random rotations {} times, average degeneracy is {}-fold".format(cnt,s/max(cnt,1)))
        print_my_name_end()
        return self._UU

    @lazy_property.LazyProperty
    def D_H(self):
        return -self.Xbar('Ham',1)*self.dEig_inv[:, :,:,None]

    @lazy_property.LazyProperty
    def A_H(self):
        '''Generalized Berry connection matrix, A^(H) as defined in eqn. (25) of 10.1103/PhysRevB.74.195118.'''
        return self.Xbar('AA') + 1j*self.D_H




    def _shc_B_H_einsum_opt(self, C, A, B):
        # Optimized version of C += np.einsum('knlc,klma->knmac', A, B). Used in shc_B_H.
        nw = self.system.num_wann
        for ik in range(self.nkptot):
            # Performing C[ik] += np.einsum('nlc,lma->nmac', A[ik], B[ik])
            tmp_a = np.swapaxes(A[ik], 1, 2) # nlc -> ncl
            tmp_a = np.reshape(tmp_a, (nw*3, nw)) # ncl -> (nc)l
            tmp_b = np.reshape(B[ik], (nw, nw*3)) # lma -> l(ma)
            tmp_c = tmp_a @ tmp_b # (nc)l, l(ma) -> (nc)(ma)
            tmp_c = np.reshape(tmp_c, (nw, 3, nw, 3)) # (nc)(ma) -> ncma
            C[ik] += np.transpose(tmp_c, (0, 2, 3, 1)) # ncma -> nmac

    @lazy_property.LazyProperty
    def shc_B_H(self):
        SH_H = self._R_to_k_H(self.SH_R, hermitean=False)
        shc_K_H = -1j*self._R_to_k_H(self.SR_R, hermitean=False)
        self._shc_B_H_einsum_opt(shc_K_H, self.Xbar('SS'), self.D_H)
        shc_L_H = -1j*self._R_to_k_H(self.SHR_R, hermitean=False)
        self._shc_B_H_einsum_opt(shc_L_H, SH_H, self.D_H)
        return (self.delE_K[:,np.newaxis,:,:,np.newaxis]*self.Xbar('SS')[:,:,:,np.newaxis,:] +
            self.E_K[:,np.newaxis,:,np.newaxis,np.newaxis]*shc_K_H[:,:,:,:,:] - shc_L_H)
#end SHC
