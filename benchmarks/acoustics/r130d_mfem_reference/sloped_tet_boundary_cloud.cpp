// Reuse the exact audited room construction and binary scalar writer.
#define main r130d_unused_point_main
#include "sloped_tet_point_cloud.cpp"
#undef main

int main(int argc,char *argv[])
{
   try
   {
      int order=2,refinement=2,quadrature=12;std::string output;
      for(int i=1;i<argc;++i)
      {
         if(i+1>=argc)throw std::runtime_error("missing boundary argument");
         const std::string arg=argv[i],value=argv[++i];
         if(arg=="--order")order=std::stoi(value);
         else if(arg=="--uniform-refinements")refinement=std::stoi(value);
         else if(arg=="--quadrature-order")quadrature=std::stoi(value);
         else if(arg=="--output")output=value;
         else throw std::runtime_error("unknown boundary argument");
      }
      if(output.empty() || order<1 || refinement<0 || refinement>4 || quadrature<2 || quadrature>24
         || std::pow(std::ldexp(1.0,refinement)*order+1,3)>40000)
         throw std::runtime_error("boundary allocation/input budget exceeded");
      if(std::ifstream(output,std::ios::binary).good())throw std::runtime_error("boundary output exists");
      mfem::Mesh mesh=BuildSlopedRoom();
      for(int i=0;i<refinement;++i)mesh.UniformRefinement();
      mfem::H1_FECollection collection(order,3);mfem::FiniteElementSpace fes(&mesh,&collection);
      std::uint32_t count=0;
      for(int i=0;i<mesh.GetNBE();++i)
         count+=mfem::IntRules.Get(fes.GetBE(i)->GetGeomType(),quadrature).GetNPoints();
      std::ofstream os(output,std::ios::binary);os.write("R130DB01",8);
      const std::uint32_t header[]={1,static_cast<std::uint32_t>(fes.GetVSize()),
         static_cast<std::uint32_t>(order),static_cast<std::uint32_t>(refinement),
         static_cast<std::uint32_t>(quadrature),count};WriteRaw(os,header,6);
      mfem::Array<int> dofs;mfem::Vector shape,xyz(3),normal(3);
      for(int i=0;i<mesh.GetNBE();++i)
      {
         const auto *fe=fes.GetBE(i);auto *T=mesh.GetBdrElementTransformation(i);
         fes.GetBdrElementDofs(i,dofs);shape.SetSize(dofs.Size());
         const auto &rule=mfem::IntRules.Get(fe->GetGeomType(),quadrature);
         for(int k=0;k<rule.GetNPoints();++k)
         {
            const auto &ip=rule.IntPoint(k);T->SetIntPoint(&ip);T->Transform(ip,xyz);
            mfem::CalcOrtho(T->Jacobian(),normal);normal/=normal.Norml2();
            const double orient=normal[0]*(xyz[0]-2)+normal[1]*(xyz[1]-2)+normal[2]*(xyz[2]-1.75);
            if(orient<0)normal*=-1.;
            fe->CalcShape(ip,shape);
            const double weight=ip.weight*T->Weight();
            WriteRaw(os,xyz.GetData(),3);WriteRaw(os,normal.GetData(),3);WriteRaw(os,&weight,1);
            const std::uint32_t size=dofs.Size();WriteRaw(os,&size,1);
            WriteRaw(os,dofs.GetData(),size);WriteRaw(os,shape.GetData(),size);
         }
      }
      return 0;
   }
   catch(const std::exception &error){std::cerr<<error.what()<<std::endl;return 1;}
}
